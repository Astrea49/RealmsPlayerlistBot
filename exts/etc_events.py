import asyncio
import contextlib
import importlib
import os

import elytra
import interactions as ipy
import msgspec

import common.models as models
import common.utils as utils


class EtcEvents(ipy.Extension):
    def __init__(self, bot: utils.RealmBotBase) -> None:
        self.bot: utils.RealmBotBase = bot
        self.update_tokens.start()

    def drop(self) -> None:
        self.update_tokens.stop()
        super().drop()

    @ipy.listen("guild_join")
    async def on_guild_join(self, event: ipy.events.GuildJoin) -> None:
        if not self.bot.is_ready:
            return

        if int(event.guild_id) in self.bot.blacklist:
            await self.bot.http.leave_guild(event.guild_id)
            return

        if not await models.GuildConfig.prisma().count(
            where={"guild_id": int(event.guild_id)}
        ):
            await models.GuildConfig.prisma().create({"guild_id": int(event.guild_id)})

    @ipy.listen("guild_left")
    async def on_guild_left(self, event: ipy.events.GuildLeft) -> None:
        if not self.bot.is_ready:
            return

        if config := await models.GuildConfig.prisma().find_unique(
            where={"guild_id": int(event.guild_id)}
        ):
            if (
                config.realm_id
                and await models.GuildConfig.prisma().count(
                    where={
                        "realm_id": config.realm_id,
                        "guild_id": {"not": int(event.guild_id)},
                    }
                )
                == 1
            ):
                # don't want to keep around entries we no longer need, so delete them
                await models.PlayerSession.prisma().delete_many(
                    where={"realm_id": config.realm_id}
                )
                # also attempt to leave the realm cus why not
                with contextlib.suppress(elytra.MicrosoftAPIException):
                    await self.bot.realms.leave_realm(config.realm_id)

            await models.GuildConfig.prisma().delete(
                where={"guild_id": int(event.guild_id)}
            )

    def _update_tokens(self) -> None:
        with open(os.environ["XAPI_TOKENS_LOCATION"], mode="wb") as f:
            f.write(msgspec.json.encode(self.bot.xbox.auth_mgr.oauth))

    @ipy.Task.create(ipy.IntervalTrigger(hours=6))
    async def update_tokens(self) -> None:
        await asyncio.to_thread(self._update_tokens)


def setup(bot: utils.RealmBotBase) -> None:
    importlib.reload(utils)
    EtcEvents(bot)
