import datetime
import importlib
import json
import os
import time

import aiohttp
import nextcord
from nextcord.ext import commands

import common.utils as utils


class GeneralCMDS(commands.Cog):
    def __init__(self, bot):
        self.bot: commands.Bot = bot

    async def pastebin_cache(self, season):
        current_time = nextcord.utils.utcnow()

        if self.bot.pastebins.get(season):
            entry = self.bot.pastebins[season]

            four_hours = datetime.timedelta(hours=4)
            four_hours_ago = current_time - four_hours

            if entry["time"] > four_hours_ago:
                return entry

        return None

    async def post_paste(self, title, content):
        headers = {
            "Authorization": f"Token {os.environ.get('GLOT_KEY')}",
            "Content-type": "application/json",
        }
        data = {
            "language": "plaintext",
            "title": f"{title}",
            "public": False,
            "files": [{"name": "main.txt", "content": f"{content}"}],
        }
        url = "https://snippets.glot.io/snippets"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, data=json.dumps(data)
            ) as resp:
                if resp.status == 200:
                    resp_json = await resp.json()
                    snippet_id = resp_json["id"]
                    return f"https://glot.io/snippets/{snippet_id}"
                else:
                    text = await resp.text()
                    await utils.msg_to_owner(self.bot, f"{resp.status}\n{text}")
                    return "ERROR, contact Astrea."

    @commands.command()
    async def ping(self, ctx):
        """Pings the bot. Great way of finding out if the bot’s working correctly, but otherwise has no real use."""
        start_time = time.perf_counter()
        ping_discord = round((self.bot.latency * 1000), 2)

        mes = await ctx.send(
            f"Pong!\n`{ping_discord}` ms from Discord.\nCalculating personal ping..."
        )

        end_time = time.perf_counter()
        ping_personal = round(((end_time - start_time) * 1000), 2)

        await mes.edit(
            content=f"Pong!\n`{ping_discord}` ms from Discord.\n`{ping_personal}` ms personally."
        )

    @commands.command(aliases=["check_season", "season_stats"])
    async def check_stats(self, ctx: commands.Context, season):
        """Checks how many people have a season role and gives a list of those people.
        The season specified... well, if you have roles that follow a 'Season X' format, where X is a number \
            or the like, then you would put what you would put in X, if that makes sense.
        Might not be fully accurate, as the bot does a tiny bit of caching here and there."""
        guild_entry = self.bot.config[str(ctx.guild.id)]
        season_x_role = nextcord.utils.get(
            ctx.guild.roles, name=guild_entry["season_role"].replace("X", season)
        )

        if season_x_role is None:
            await ctx.send("Invalid season number!")
        else:
            cache = await self.pastebin_cache(season)
            if not cache:
                list_of_people = [
                    f"{member.display_name} || {member} || {member.id}"
                    for member in ctx.guild.members
                    if member._roles.has(season_x_role.id)
                ]

                count = len(list_of_people)

                title = f"Query about people that have the {season_x_role.name} role:"
                str_of_people = "".join(name + "\n" for name in list_of_people)
                url = await self.post_paste(title, str_of_people)

                self.bot.pastebins[season] = {
                    "time": nextcord.utils.utcnow(),
                    "url": url,
                    "count": count,
                }

            else:
                url = cache["url"]
                count = cache["count"]
            stats_embed = nextcord.Embed(
                title=f"There are {count} people that have the {season_x_role.name} role.",
                colour=ctx.bot.color,
                description=f"List of members: {url}",
            )

            stats_embed.set_author(
                name=f"{ctx.guild.me.display_name}",
                icon_url=str(
                    ctx.guild.me.display_avatar.replace(format="jpg", size=128)
                ),
            )

            await ctx.send(embed=stats_embed)


def setup(bot):
    importlib.reload(utils)
    bot.add_cog(GeneralCMDS(bot))
