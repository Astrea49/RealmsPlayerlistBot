import importlib
import os
import typing

import aiohttp
import attrs
import interactions as ipy

import common.utils as utils


@attrs.define(kw_only=True)
class VoteHandler:
    name: str = attrs.field()
    base_url: str = attrs.field()
    headers: dict[str, str] = attrs.field()
    data_url: str = attrs.field()
    data_callback: typing.Callable[[int, int], dict[str, typing.Any]] = attrs.field()
    vote_url: typing.Optional[str] = attrs.field()


class Voting(ipy.Extension):
    def __init__(self, bot: utils.RealmBotBase) -> None:
        self.bot: utils.RealmBotBase = bot
        self.name = "Voting"

        self.shard_count = len(bot.shards)

        self.handlers: list[VoteHandler] = []

        if os.environ.get("TOP_GG_TOKEN"):
            self.handlers.append(
                VoteHandler(
                    name="Top.gg",
                    base_url="https://top.gg/api",
                    headers={"Authorization": os.environ["TOP_GG_TOKEN"]},
                    data_url="/bots/{bot_id}/stats",
                    data_callback=lambda guild_count, shard_count: {
                        "server_count": guild_count,
                        "shard_count": shard_count,
                    },
                    vote_url="https://top.gg/bot/{bot_id}/vote **(prefered)**",
                )
            )

        if os.environ.get("DISCORDSCOM_TOKEN"):
            self.handlers.append(
                VoteHandler(
                    name="Discords.com",
                    base_url="https://discords.com/bots/api",
                    headers={"Authorization": os.environ["DISCORDSCOM_TOKEN"]},
                    data_url="/bot/{bot_id}",
                    data_callback=lambda guild_count, _: {"server_count": guild_count},
                    vote_url="https://discords.com/bots/bot/{bot_id}",
                ),
            )

        if os.environ.get("DBL_TOKEN"):
            self.handlers.append(
                VoteHandler(
                    name="Discord Bot List",
                    base_url="https://discordbotlist.com/api/v1",
                    headers={"Authorization": os.environ["DBL_TOKEN"]},
                    data_url="/bots/{bot_id}/stats",
                    data_callback=lambda guild_count, _: {"guilds": guild_count},
                    vote_url=(
                        "https://discordbotlist.com/bots/realms-playerlist-bot/upvote"
                    ),
                )
            )

        if os.environ.get("DISCORD_BOTS_TOKEN"):
            self.handlers.append(
                VoteHandler(
                    name="Discord Bots",
                    base_url="https://discord.bots.gg/api/v1",
                    headers={
                        "Authorization": os.environ["DISCORD_BOTS_TOKEN"],
                        "Content-Type": "application/json",
                    },
                    data_url="/bots/{bot_id}/stats",
                    data_callback=lambda guild_count, shard_count: {
                        "guildCount": guild_count,
                        "shardCount": shard_count,
                    },
                    vote_url=(None),
                )
            )

        if not self.handlers:
            raise ValueError("No voting handlers were configured.")

        self.autopost_guild_count.start()

    def drop(self) -> None:
        self.autopost_guild_count.stop()
        super().drop()

    @ipy.Task.create(ipy.IntervalTrigger(minutes=30))
    async def autopost_guild_count(self) -> None:
        server_count = len(self.bot.guilds)

        for handler in self.handlers:
            async with self.bot.session.post(
                f"{handler.base_url}{handler.data_url.format(bot_id=self.bot.user.id)}",
                json=handler.data_callback(server_count, self.shard_count),
                headers=handler.headers,
            ) as r:
                try:
                    r.raise_for_status()
                except aiohttp.ClientResponseError as e:
                    await utils.error_handle(e)

    @ipy.slash_command(
        name="vote",
        description="Vote for the bot.",
    )
    async def vote(self, ctx: utils.RealmContext) -> None:
        website_votes: list[str] = [
            f"**{handler.name}** - {handler.vote_url.format(bot_id=self.bot.user.id)}"
            for handler in self.handlers
            if handler.vote_url
        ]
        await ctx.send(
            embeds=ipy.Embed(
                title="Vote for the bot",
                description="\n".join(website_votes),
                color=self.bot.color,
                timestamp=ctx.id.created_at,
            )
        )


def setup(bot: utils.RealmBotBase) -> None:
    importlib.reload(utils)
    Voting(bot)
