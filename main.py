"""
Copyright 2020-2025 AstreaTSS.
This file is part of the Realms Playerlist Bot.

The Realms Playerlist Bot is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.

The Realms Playerlist Bot is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along with the Realms
Playerlist Bot. If not, see <https://www.gnu.org/licenses/>.
"""

import asyncio
import contextlib
import datetime
import functools
import logging
import os
import typing
import uuid
from collections import defaultdict

import rpl_config

rpl_config.load()

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

import aiohttp
import elytra
import interactions as ipy
import orjson
import sentry_sdk
import valkey.asyncio as aiovalkey
from interactions.api.events.processors import Processor
from interactions.api.gateway.state import ConnectionState
from interactions.ext import prefixed_commands as prefixed
from prisma import Prisma

import common.classes as cclasses
import common.help_tools as help_tools
import common.models as models
import common.utils as utils

if typing.TYPE_CHECKING:
    import discord_typings


def default_sentry_filter(
    event: dict[str, typing.Any], hint: dict[str, typing.Any]
) -> typing.Optional[dict[str, typing.Any]]:
    if "log_record" in hint:
        record: logging.LogRecord = hint["log_record"]
        if "interactions" in record.name or "realms_bot" in record.name:
            # there are some logging messages that are not worth sending to sentry
            if ": 403" in record.message:
                return None
            if ": 404" in record.message:
                return None
            if record.message.startswith("Ignoring exception in "):
                return None
            if record.message.startswith("Unsupported channel type for "):
                # please shut up
                return None

    if "exc_info" in hint:
        exc_type, exc_value, tb = hint["exc_info"]
        if isinstance(exc_value, KeyboardInterrupt):
            #  We don't need to report a ctrl+c
            return None
    return event


class MyHookedTask(ipy.Task):
    def on_error_sentry_hook(self: ipy.Task, error: Exception) -> None:
        scope = sentry_sdk.Scope.get_current_scope()

        if isinstance(self.callback, functools.partial):
            scope.set_tag("task", self.callback.func.__name__)
        else:
            scope.set_tag("task", self.callback.__name__)

        scope.set_tag("iteration", self.iteration)
        sentry_sdk.capture_exception(error)


# im so sorry
if not utils.FEATURE("PRINT_TRACKBACK_FOR_ERRORS") and utils.SENTRY_ENABLED:
    ipy.Task.on_error_sentry_hook = MyHookedTask.on_error_sentry_hook
    sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], before_send=default_sentry_filter)


# ipy used to implement this, but strayed away from it
# im adding it back in just in case
async def basic_guild_check(ctx: ipy.SlashContext) -> bool:
    return True if ctx.command.dm_permission else ctx.guild_id is not None


class RealmsPlayerlistBot(utils.RealmBotBase):
    @ipy.listen("startup")
    async def on_startup(self) -> None:
        # frankly, this event isn't needed anymore,
        # but too many things depend on fully_ready being set for me to remove it
        self.fully_ready.set()

    @ipy.listen("ready")
    async def on_ready(self) -> None:
        # dms bot owner on every ready noting if the bot is coming up or reconnecting
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

        # good time to change splash text too
        activity = ipy.Activity(
            name="Splash Text",
            type=ipy.ActivityType.CUSTOM,
            state="Watching Realms | playerlist.astrea.cc",
        )
        await self.change_presence(activity=activity)

    @ipy.listen("resume")
    async def on_resume_func(self) -> None:
        activity = ipy.Activity(
            name="Splash Text",
            type=ipy.ActivityType.CUSTOM,
            state="Watching Realms | playerlist.astrea.cc",
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
            # process slash command permissions for this guild for the help command
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

    @ipy.listen(ipy.events.ShardDisconnect)
    async def shard_disconnect(self, event: ipy.events.ShardDisconnect) -> None:
        # this usually means disconnect with an error, which is very unusual
        # thus, we should log this and attempt to restart
        try:
            await self.wait_for(ipy.events.Disconnect, timeout=1)
        except TimeoutError:
            return

        await self.bot_owner.send(
            f"Shard {event.shard_id} disconnected due to an error. Attempting restart."
        )
        await asyncio.sleep(5)

        self._connection_states[event.shard_id] = ConnectionState(
            self, self.intents, event.shard_id
        )
        self.create_task(self._connection_states[event.shard_id].start())

    # guild related stuff so that no caching of guilds is even attempted
    # this code is cursed, im aware

    @ipy.listen()
    async def _on_websocket_ready(self, event: ipy.events.RawGatewayEvent) -> None:
        connection_data = event.data
        expected_guilds = {int(guild["id"]) for guild in connection_data["guilds"]}
        self.unavailable_guilds |= expected_guilds
        await super()._on_websocket_ready(self, event)

    @property
    def guild_count(self) -> int:
        return len(self.user._guild_ids or ())

    @Processor.define()
    async def _on_raw_guild_create(self, event: "ipy.events.RawGatewayEvent") -> None:
        guild_id: int = int(event.data["id"])
        new_guild = guild_id not in self.user._guild_ids
        self.unavailable_guilds.discard(guild_id)

        if new_guild:
            self.user._guild_ids.add(guild_id)
            self.dispatch(ipy.events.GuildJoin(guild_id))
        else:
            self.dispatch(ipy.events.GuildAvailable(guild_id))

    @Processor.define()
    async def _on_raw_guild_update(self, _: "ipy.events.RawGatewayEvent") -> None:
        # yes, this is funny, but we never use guild updates and it would only add
        # to our cache
        return

    @Processor.define()
    async def _on_raw_guild_delete(self, event: "ipy.events.RawGatewayEvent") -> None:
        guild_id: int = int(event.data["id"])

        if event.data.get("unavailable", False):
            self.unavailable_guilds.add(guild_id)
            self.dispatch(ipy.events.GuildUnavailable(guild_id))
        else:
            self.user._guild_ids.discard(guild_id)
            self.dispatch(ipy.events.GuildLeft(guild_id, None))  # type: ignore

    @Processor.define()
    async def _on_raw_message_create(self, event: "ipy.events.RawGatewayEvent") -> None:
        # needs to be custom defined otherwise it will try to cache the guild
        msg = self.cache.place_message_data(event.data)
        if not msg._guild_id and event.data.get("guild_id"):
            msg._guild_id = event.data["guild_id"]

        if not self.cache.get_channel(msg._channel_id):
            self.cache.channel_cache[ipy.to_snowflake(msg._channel_id)] = (
                utils.partial_channel(self, msg._channel_id)
            )

        self.dispatch(ipy.events.MessageCreate(msg))

    def create_task(self, coro: typing.Coroutine) -> asyncio.Task:
        # see the "important" note below for why we do this (to prevent early gc)
        # https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
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
            self.create_task(self._cache_interactions(warn_missing=False))

    def add_interaction(self, command: ipy.InteractionCommand) -> bool:
        result = super().add_interaction(command)
        if result and self.enforce_interaction_perms:
            command.checks.append(basic_guild_check)
        return result

    async def stop(self) -> None:
        await bot.openxbl_session.close()
        await bot.session.close()
        await bot.xbox.close()
        await bot.realms.close()
        await bot.db.disconnect()
        await bot.valkey.aclose(close_connection_pool=True)

        return await super().stop()


intents = ipy.Intents.new(
    guilds=True,
    messages=True,
)
mentions = ipy.AllowedMentions.all()

bot = RealmsPlayerlistBot(
    activity=ipy.Activity(
        name="Status", type=ipy.ActivityType.CUSTOM, state="Loading..."
    ),
    status=ipy.Status.IDLE,
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
    # we do not need many of these
    message_cache=ipy.utils.TTLCache(10, 10, 50),
    role_cache=ipy.utils.TTLCache(60, 100, 200),
    channel_cache=ipy.utils.TTLCache(60, 200, 400),
    user_cache=ipy.utils.TTLCache(60, 200, 400),
    member_cache=ipy.utils.TTLCache(60, 200, 400),
    # do not need at all
    voice_state_cache=ipy.utils.NullCache(),
    user_guilds=ipy.utils.NullCache(),
    guild_cache=ipy.utils.NullCache(),
    dm_channels=ipy.utils.NullCache(),
    logger=logger,
)
prefixed.setup(bot, prefixed_context=utils.RealmPrefixedContext)
bot.guild_event_timeout = (
    -1
)  # internal variable to control how long to wait for each guild object, but we dont care for them
bot.unavailable_guilds = set()
bot.init_load = True
bot.bot_owner = None  # type: ignore
bot.color = ipy.Color(int(os.environ["BOT_COLOR"]))  # c156e0, aka 12670688
bot.online_cache = defaultdict(set)
bot.slash_perms_cache = defaultdict(dict)
bot.live_playerlist_store = defaultdict(set)
bot.player_watchlist_store = defaultdict(set)
bot.uuid_cache = defaultdict(lambda: str(uuid.uuid4()))
bot.mini_commands_per_scope = {}
bot.offline_realms = cclasses.OrderedSet()
bot.dropped_offline_realms = set()
bot.fetch_devices_for = set()
bot.background_tasks = set()
bot.blacklist = set()


async def start() -> None:
    db = Prisma(
        auto_register=True,
        datasource={"url": os.environ["DB_URL"]},
        http={"http2": True},
    )
    await db.connect()
    bot.db = db

    bot.valkey = aiovalkey.Valkey.from_url(
        os.environ["VALKEY_URL"],
        decode_responses=True,
    )

    if blacklist_raw := await bot.valkey.get("rpl-blacklist"):
        bot.blacklist = set(orjson.loads(blacklist_raw))
    else:
        bot.blacklist = set()
        await bot.valkey.set("rpl-blacklist", orjson.dumps([]))

    # mark players as offline if they were online more than 5 minutes ago
    five_minutes_ago = ipy.Timestamp.utcnow() - datetime.timedelta(minutes=5)
    num_updated = await models.PlayerSession.prisma().update_many(
        data={"online": False},
        where={
            "online": True,
            "last_seen": {"lt": five_minutes_ago},
        },
    )
    if num_updated > 0:
        # we've reset all online entries, reset live online channels too
        for config in await models.GuildConfig.prisma().find_many(
            where={"NOT": [{"live_online_channel": None}]}
        ):
            await bot.valkey.hset(config.live_online_channel, "xuids", "")
            await bot.valkey.hset(config.live_online_channel, "gamertags", "")

    # add all online players to the online cache
    for player in await models.PlayerSession.prisma().find_many(where={"online": True}):
        bot.uuid_cache[player.realm_xuid_id] = player.custom_id
        bot.online_cache[int(player.realm_id)].add(player.xuid)

    if utils.FEATURE("HANDLE_MISSING_REALMS"):
        async for realm_id in bot.valkey.scan_iter("missing-realm-*"):
            bot.offline_realms.add(int(realm_id.removeprefix("missing-realm-")))

    for config in await models.GuildConfig.prisma().find_many(
        where={
            "NOT": [
                {
                    "realm_id": None,
                    "playerlist_chan": None,
                    "player_watchlist": {"is_empty": True},
                },
            ]
        }
    ):
        for player_xuid in config.player_watchlist:
            bot.player_watchlist_store[f"{config.realm_id}-{player_xuid}"].add(
                config.guild_id
            )

    # add info for who has premium features on and has valid premium
    for config in await models.GuildConfig.prisma().find_many(
        where={
            "NOT": [
                {"premium_code_id": None, "realm_id": None},
            ],
            "OR": [  # i honestly still cant believe this is all typehinted
                {"premium_code": {"is": {"expires_at": None}}},
                {
                    "premium_code": {
                        "is": {
                            "expires_at": {
                                "gt": (
                                    ipy.Timestamp.utcnow() - datetime.timedelta(days=1)
                                )
                            }
                        }
                    }
                },
            ],
        },
        include={"premium_code": True},
    ):
        if config.playerlist_chan and config.live_playerlist:
            bot.live_playerlist_store[config.realm_id].add(config.guild_id)  # type: ignore
        if config.fetch_devices:
            bot.fetch_devices_for.add(config.realm_id)

    bot.fully_ready = asyncio.Event()
    bot.pl_sem = asyncio.Semaphore(12)  # TODO: maybe increase this?

    bot.xbox = await elytra.XboxAPI.from_file(
        os.environ["XBOX_CLIENT_ID"],
        os.environ["XBOX_CLIENT_SECRET"],
        os.environ["XAPI_TOKENS_LOCATION"],
    )
    bot.realms = await elytra.BedrockRealmsAPI.from_file(
        os.environ["XBOX_CLIENT_ID"],
        os.environ["XBOX_CLIENT_SECRET"],
        os.environ["XAPI_TOKENS_LOCATION"],
    )
    bot.own_gamertag = bot.xbox.auth_mgr.xsts_token.gamertag

    headers = {
        "X-Authorization": os.environ["OPENXBL_KEY"],
        "Accept": "application/json",
        "Accept-Language": "en-US",
    }
    bot.openxbl_session = aiohttp.ClientSession(
        headers=headers,
        response_class=cclasses.BetterResponse,
        json_serialize=lambda x: orjson.dumps(x).decode(),
    )
    bot.session = aiohttp.ClientSession(
        response_class=cclasses.BetterResponse,
        json_serialize=lambda x: orjson.dumps(x).decode(),
    )

    ext_list = utils.get_all_extensions(os.environ["DIRECTORY_OF_BOT"])
    for ext in ext_list:
        # skip loading voting ext if token doesn't exist
        if "voting" in ext and not utils.VOTING_ENABLED:
            continue

        if not utils.FEATURE("AUTORUNNER") and "autorun" in ext:
            continue

        if not utils.FEATURE("ETC_EVENTS") and "etc" in ext:
            continue

        try:
            bot.load_extension(ext)
        except ipy.errors.ExtensionLoadException:
            raise

    with contextlib.suppress(asyncio.CancelledError):
        await bot.astart(os.environ["MAIN_TOKEN"])


if __name__ == "__main__":
    run_method = asyncio.run

    # use uvloop if possible
    with contextlib.suppress(ImportError):
        import uvloop  # type: ignore

        run_method = uvloop.run

    if os.environ.get("DOCKER_MODE") == "True" and utils.FEATURE(
        "RUN_MIGRATIONS_AUTOMATICALLY"
    ):
        import subprocess
        import sys

        subprocess.run(
            [sys.executable, "-m", "prisma", "migrate", "deploy"],
            check=True,
            env={"DB_URL": os.environ["DB_URL"]},
        )

    run_method(start())
