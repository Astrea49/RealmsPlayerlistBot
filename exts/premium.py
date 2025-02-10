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

import importlib
import io
import os
import typing

import interactions as ipy
import tansy

import common.classes as cclasses
import common.models as models
import common.playerlist_utils as pl_utils
import common.premium_utils as premium_utils
import common.utils as utils


def premium_check[T: (
    ipy.BaseCommand,
    ipy.const.AsyncCallable,
)]() -> typing.Callable[[T], T]:
    async def check(ctx: utils.RealmContext) -> bool:
        config = await ctx.fetch_config()

        if not config.valid_premium:
            raise utils.CustomCheckFailure(
                "This server does not have premium activated! Check out"
                f" {ctx.bot.mention_command('premium info')} for more information"
                " about it."
            )

        return True

    return ipy.check(check)


class PremiumHandling(utils.Extension):
    def __init__(self, bot: utils.RealmBotBase) -> None:
        self.bot: utils.RealmBotBase = bot
        self.name = "Premium Handling"

    @tansy.slash_command(
        name="generate-code",
        description="Generates a premium code. Can only be used by the bot's owner.",
        scopes=[utils.DEV_GUILD_ID],
        default_member_permissions=ipy.Permissions.ADMINISTRATOR,
    )
    async def generate_code(
        self,
        ctx: ipy.InteractionContext,
        max_uses: int = tansy.Option("How many uses the code has.", default=2),
        user_id: typing.Optional[str] = tansy.Option(
            "The user ID this is tied to if needed.", default=None
        ),
    ) -> None:
        # mind you, it isn't TOO important that this is secure - really, i just want
        # to make sure your average tech person couldn't brute force a code
        # regardless, we do try to use aes here just in case

        actual_user_id = int(user_id) if user_id is not None else None

        code = premium_utils.full_code_generate(max_uses, user_id)
        encrypted_code = await premium_utils.encrypt_input(code)

        await models.PremiumCode.prisma().create(
            data={
                "code": encrypted_code,
                "user_id": actual_user_id,
                "max_uses": max_uses,
            }
        )
        await ctx.send(f"Code created!\nCode: `{code}`")

    premium = tansy.TansySlashCommand(
        name="premium",
        description="Handles the configuration for Realms Playerlist Premium.",
        default_member_permissions=ipy.Permissions.MANAGE_GUILD,
        dm_permission=False,
    )

    @premium.subcommand(
        sub_cmd_name="redeem",
        sub_cmd_description=(
            "Redeems the premium code for the server this command is run in."
        ),
    )
    async def redeem_premium(
        self, ctx: utils.RealmContext, code: str = tansy.Option("The code for premium.")
    ) -> None:
        encrypted_code: str | None = None

        if maybe_valid_code := premium_utils.full_code_validate(code, ctx.author.id):
            encrypted_code = await premium_utils.encrypt_input(maybe_valid_code)
        else:
            if 20 < len(code) < 24:  # support old codes
                encrypted_code = await premium_utils.encrypt_input(code)
            if (
                not encrypted_code
                or premium_utils.bytestring_length_decode(encrypted_code) != 22
            ):
                raise ipy.errors.BadArgument(
                    f'Invalid code: "{code}". Are you sure this is the correct code and'
                    " that you typed it in correctly?"
                )

        code_obj = await models.PremiumCode.prisma().find_first(
            where={"code": encrypted_code}
        )

        if not code_obj:
            raise ipy.errors.BadArgument(
                f'Invalid code: "{code}". Are you sure this is the correct code and'
                " that you typed it in correctly?"
            )

        if not code_obj.valid_code:
            raise ipy.errors.BadArgument(
                f'Invalid code: "{code}". Are you sure this is the correct code and'
                " that you typed it in correctly?"
            )

        if code_obj.user_id and ctx.author.id != code_obj.user_id:
            raise ipy.errors.BadArgument(
                f'Invalid code: "{code}". Are you sure this is the correct code and'
                " that you typed it in correctly?"
            )

        if code_obj.uses >= code_obj.max_uses:
            raise ipy.errors.BadArgument("This code cannot be redeemed anymore.")

        config = await ctx.fetch_config()

        if config.premium_code and config.premium_code.code == code:
            raise ipy.errors.BadArgument("This code has already been redeemed here.")

        await models.GuildConfig.prisma().update(
            data={"premium_code": {"connect": {"id": code_obj.id}}},
            where={"guild_id": ctx.guild_id},
        )

        code_obj = await models.PremiumCode.prisma().update(
            data={"uses": {"increment": 1}}, where={"id": code_obj.id}
        )
        if typing.TYPE_CHECKING:
            assert code_obj is not None

        remaining_uses = code_obj.max_uses - code_obj.uses
        uses_str = "uses" if remaining_uses != 1 else "use"

        await ctx.send(
            embeds=utils.make_embed(
                "Code redeemed for this server!\nThis code has"
                f" {remaining_uses} {uses_str} remaining."
            )
        )

    @premium.subcommand(
        sub_cmd_name="live-playerlist",
        sub_cmd_description=(
            "Turns on or off the live playerlist. Can only be run for servers with"
            " premium activated."
        ),
    )
    @premium_check()
    async def toggle_live_playerlist(
        self,
        ctx: utils.RealmContext,
        toggle: bool = tansy.Option("Should it be on (true) or off (false)?"),
    ) -> None:
        config = await ctx.fetch_config()

        if not (config.realm_id and config.playerlist_chan):
            raise utils.CustomCheckFailure(
                "You need to link your Realm and set a playerlist channel before"
                " running this."
            )

        if toggle:
            self.bot.live_playerlist_store[config.realm_id].add(config.guild_id)
        else:
            self.bot.live_playerlist_store[config.realm_id].discard(config.guild_id)

        config.live_playerlist = toggle
        await config.save()
        await ctx.send(
            embeds=utils.make_embed(
                f"Turned {utils.toggle_friendly_str(toggle)} the live playerlist."
            )
        )

    @premium.subcommand(
        sub_cmd_name="send-live-online-list",
        sub_cmd_description=(
            "Sends out a message that updates with currently online players to the"
            " current channel. Premium only."
        ),
    )
    @ipy.auto_defer(ephemeral=True)
    @premium_check()
    async def send_live_online_list(self, ctx: utils.RealmContext) -> None:
        config = await ctx.fetch_config()

        if not (config.realm_id and config.playerlist_chan):
            raise utils.CustomCheckFailure(
                "You need to link your Realm and set a playerlist channel before"
                " running this."
            )
        if not config.live_playerlist:
            raise utils.CustomCheckFailure(
                "You need to turn on the live playerlist to use this as of right now."
            )

        if not ctx.app_permissions:
            raise utils.CustomCheckFailure(
                "Could not resolve permissions for this channel."
            )

        if (
            ipy.Permissions.VIEW_CHANNEL
            | ipy.Permissions.SEND_MESSAGES
            | ipy.Permissions.READ_MESSAGE_HISTORY
            | ipy.Permissions.EMBED_LINKS
            not in ctx.app_permissions
        ):
            raise utils.CustomCheckFailure(
                "I need the `View Channel`, `Send Messages`, `Read Message History`,"
                " and `Embed Links` permissions in this channel to send out and be able"
                " to edit the live online list.\n*As for how this message has been"
                " sent, slash commands are weird. I still need those permissions"
                " regardless.*"
            )

        player_sessions = await models.PlayerSession.prisma().find_many(
            where={"realm_id": config.realm_id, "online": True}
        )
        playerlist = await pl_utils.fill_in_gamertags_for_sessions(
            self.bot,
            player_sessions,
            bypass_cache=config.fetch_devices,
        )

        online_list = sorted(
            (p for p in playerlist if p.online),
            key=lambda k: k.display(config.nicknames.get(k.xuid)).lower(),
        )
        online_str = "\n".join(
            p.display(config.nicknames.get(p.xuid)) for p in online_list
        )
        xuids = ",".join(p.xuid for p in online_list)

        embed = ipy.Embed(
            title=f"{len(online_list)}/10 people online",
            description=online_str or "*No players online.*",
            color=self.bot.color,
            timestamp=ipy.Timestamp.utcnow(),
        )
        embed.set_footer("As of")

        try:
            msg = await ctx.channel.send(embed=embed)
        except ipy.errors.HTTPException:
            raise utils.CustomCheckFailure(
                "An error occured when trying to send the live online list. Make sure"
                " the bot has `View Channel`, `Send Messages`, `Read Message"
                " History`, and `Embed Links` enabled for this channel."
            ) from None

        config.live_online_channel = f"{msg._channel_id}|{msg.id}"
        await config.save()

        await self.bot.valkey.hset(config.live_online_channel, "xuids", xuids)
        await self.bot.valkey.hset(config.live_online_channel, "gamertags", online_str)

        await ctx.send(embeds=utils.make_embed("Done!"), ephemeral=True)

    @staticmethod
    def button_check(author_id: int) -> typing.Callable[..., bool]:
        def _check(event: ipy.events.Component) -> bool:
            return event.ctx.author.id == author_id

        return _check

    @premium.subcommand(
        sub_cmd_name="device-information",
        sub_cmd_description=(
            "Displays online player devices for various commands. Premium only; will"
            " slow bot."
        ),
    )
    @premium_check()
    async def toggle_device_information(
        self,
        ctx: utils.RealmContext,
        toggle: bool = tansy.Option("Should it be on (true) or off (false)?"),
    ) -> None:
        config = await ctx.fetch_config()

        if not config.realm_id:
            raise utils.CustomCheckFailure(
                "You need to link your Realm before running this."
            )
        if config.fetch_devices == toggle:
            raise ipy.errors.BadArgument("That's already the current setting.")

        if toggle:
            embed = ipy.Embed(
                title="Warning",
                description=(
                    "This will display the device the user is playing on if they are on"
                    " the Realm whenever the bot shows them.\n**However, this will make"
                    " the bot slower with certain commands**, like `/online` and"
                    " `/playerlist`, and also slow down the live playerlist slightly"
                    " (if enabled), as getting the device information requires getting"
                    " a bit more information that what is usually stored.\n*This will"
                    " also not work with every single player* - privacy settings may"
                    " make the bot unable to fetch the device.\n\n**If you wish to"
                    " continue with enabling the displaying of devices, press the"
                    " accept button.** You have 90 seconds to do so."
                ),
                timestamp=ipy.Timestamp.utcnow(),
                color=ipy.RoleColors.YELLOW,
            )

            result = ""
            event = None

            components = [
                ipy.Button(style=ipy.ButtonStyle.GREEN, label="Accept", emoji="✅"),
                ipy.Button(style=ipy.ButtonStyle.RED, label="Decline", emoji="✖️"),
            ]
            msg = await ctx.send(embed=embed, components=components)

            try:
                event = await self.bot.wait_for_component(
                    msg, components, self.button_check(ctx.author.id), timeout=90
                )

                if event.ctx.custom_id == components[1].custom_id:
                    result = "Declined displaying devices."
                else:
                    config.fetch_devices = True
                    await config.save()
                    self.bot.fetch_devices_for.add(config.realm_id)

                    result = "Turned on displaying devices."
            except TimeoutError:
                result = "Timed out."
            finally:
                embed = utils.make_embed(result)
                if event:
                    await event.ctx.send(
                        embeds=embed,
                        ephemeral=True,
                    )
                await ctx.edit(msg, embeds=embed, components=[])  # type: ignore
        else:
            config.fetch_devices = False
            await config.save()

            await ctx.send(embeds=utils.make_embed("Turned off displaying devices."))

            if not await models.GuildConfig.prisma().count(
                where={"realm_id": config.realm_id, "fetch_devices": True}
            ):
                self.bot.fetch_devices_for.discard(config.realm_id)

    @premium.subcommand(
        sub_cmd_name="export",
        sub_cmd_description=(
            "Exports all stored (~30 days) player session data for the linked Realm to"
            " a CSV."
        ),
    )
    @premium_check()
    @ipy.cooldown(ipy.Buckets.GUILD, 1, 60)
    async def export_to_csv(self, ctx: utils.RealmContext) -> None:
        config = await ctx.fetch_config()

        if not config.realm_id:
            raise utils.CustomCheckFailure(
                "You need to link your Realm before running this."
            )

        csv_entries: list[str] = ["xuid,gamertag,online,last_seen,joined_at"]

        sessions = await models.PlayerSession.prisma().find_many(
            where={"realm_id": config.realm_id, "NOT": [{"joined_at": None}]},
            order={"last_seen": "desc"},
        )
        gamertags = await pl_utils.get_xuid_to_gamertag_map(
            self.bot, list(dict.fromkeys(session.xuid for session in sessions))
        )

        for session in sessions:
            if typing.TYPE_CHECKING:
                assert session.joined_at is not None

            csv_entries.append(
                f"{session.xuid},{gamertags[session.xuid]},{session.online},"
                f"{session.last_seen.isoformat(timespec='seconds')},"
                f"{session.joined_at.isoformat(timespec='seconds')}"
            )

        if len(csv_entries) == 1:
            raise utils.CustomCheckFailure("There is no data to export for this Realm.")

        csv_str = "\n".join(csv_entries)
        csv_io = io.StringIO(csv_str)
        csv_file = ipy.File(
            csv_io,
            file_name=f"{config.realm_id}-{int(ctx.id.created_at.timestamp())}.csv",
        )

        try:
            await ctx.send(
                "Done! Please note that this file only contains raw player"
                " session data - it's up to you to process this information.",
                file=csv_file,
            )
        finally:
            csv_io.close()

    @premium.subcommand(
        sub_cmd_name="reoccurring-leaderboard",
        sub_cmd_description=(
            "Configures a leaderboard that is sent at a certain frequency. Premium"
            " only."
        ),
    )
    @premium_check()
    async def reoccurring_leaderboard(
        self,
        ctx: utils.RealmContext,
        toggle: bool = tansy.Option("Should it be turned on (true) or off (false)?"),
        frequency: int | None = tansy.Option(
            "How often the leaderboard should be sent.",
            choices=[
                ipy.SlashCommandChoice(v, k)
                for k, v in utils.REOCCURRING_LB_FREQUENCY.items()
            ],
            default=None,
        ),
        period: int | None = tansy.Option(
            "The period to gather data for each leaderboard for.",
            choices=[
                ipy.SlashCommandChoice(v, k)
                for k, v in utils.REOCCURRING_LB_PERIODS.items()
            ],
            default=None,
        ),
        channel: ipy.GuildText | None = tansy.Option(
            "The channel to send the leaderboard to. If not set, defaults to the"
            " autorunning playerlist channel.",
            converter=cclasses.ValidChannelConverter,
        ),
    ) -> None:
        config = await ctx.fetch_config()

        if toggle:
            if not (config.realm_id and config.playerlist_chan):
                raise utils.CustomCheckFailure(
                    "You need to link your Realm and set a playerlist channel before"
                    " running this."
                )

            if not frequency or not period:
                raise ipy.errors.BadArgument(
                    "You must provide a frequency and period when enabling this"
                    " feature!"
                )

            config.reoccurring_leaderboard = (frequency * 10) + period
            if channel:
                config.notification_channels["reoccurring_leaderboard"] = channel.id
            await config.save()

            await ctx.send(
                embed=utils.make_embed(
                    "Set the reoccurring leaderboard to run"
                    f" {utils.REOCCURRING_LB_FREQUENCY[frequency]} with a period of"
                    f" {utils.REOCCURRING_LB_PERIODS[period]}, sending the"
                    " leaderboard to"
                    f" <#{config.get_notif_channel('reoccurring_leaderboard')}>."
                ),
            )

        else:
            if not config.reoccurring_leaderboard:
                raise ipy.errors.BadArgument(
                    "The reoccurring leaderboard hasn't been set yet!"
                )

            config.reoccurring_leaderboard = None
            config.notification_channels.pop("reoccurring_leaderboard", None)
            await config.save()

            await ctx.send(
                embed=utils.make_embed("Disabled the reoccurring leaderboard.")
            )

    @premium.subcommand(
        sub_cmd_name="info",
        sub_cmd_description=(
            "Gives you information about Realms Playerlist Premium and how to get it."
        ),
    )
    async def premium_info(self, ctx: utils.RealmContext) -> None:
        embed = utils.make_embed(
            "To learn about Realms Playerlist Premium, what features it has, and how to"
            " get it, check out the link below.",
            title="Premium Info",
        )
        button = ipy.Button(
            style=ipy.ButtonStyle.URL,
            label="Premium Info",
            url="https://playerlist.astrea.cc/wiki/premium.html",
        )
        await ctx.send(embeds=embed, components=button)

    @ipy.listen(ipy.events.EntitlementCreate)
    async def entitlement_create(self, event: ipy.events.EntitlementCreate) -> None:
        entitlement = event.entitlement

        if (
            str(entitlement.sku_id) != os.environ.get("PREMIUM_SKU_ID")
            or not entitlement.subscription_id
            or not entitlement._guild_id
        ):
            return

        if config := await models.GuildConfig.prisma().find_first(
            where={
                "guild_id": int(entitlement._guild_id),
                "premium_code": {
                    "is": {
                        "user_id": int(entitlement._user_id),
                        "max_uses": 1,
                        "uses": 1,
                    },
                    "is_not": {"customer_id": None},
                },
            },
            include={"premium_code": True},
        ):
            # potential update from old subscription system to new one,
            # so we'll just delete the old code and update the guild config
            await models.PremiumCode.prisma().delete(
                where={"id": config.premium_code_id}
            )

        # admittedly, i really don't want to go through the effort of making
        # stuff for just entitlements, so we're going to pretend they're
        # stripe subscriptions by making a code and then using it just like normal

        code = premium_utils.full_code_generate(1, entitlement._user_id)
        encrypted_code = await premium_utils.encrypt_input(code)

        code = await models.PremiumCode.prisma().create(
            data={
                "code": encrypted_code,
                "max_uses": 1,
                "user_id": int(entitlement._user_id) if entitlement._user_id else None,
                "customer_id": str(entitlement.subscription_id),
            }
        )

        await models.GuildConfig.prisma().update(
            data={"premium_code": {"connect": {"id": code.id}}},
            where={"guild_id": int(entitlement._guild_id)},
        )
        await models.PremiumCode.prisma().update(
            data={"uses": {"increment": 1}}, where={"id": code.id}
        )

    @ipy.listen(ipy.events.EntitlementUpdate)
    async def entitlement_update(self, event: ipy.events.EntitlementUpdate) -> None:
        entitlement = event.entitlement

        if (
            str(entitlement.sku_id) != os.environ.get("PREMIUM_SKU_ID")
            or not entitlement.ends_at
            or not entitlement.subscription_id
            or not entitlement._guild_id
        ):
            return

        await models.PremiumCode.prisma().update_many(
            data={"expires_at": entitlement.ends_at},
            where={"customer_id": str(entitlement.subscription_id)},
        )

    @ipy.listen(ipy.events.EntitlementDelete)
    async def entitlement_delete(self, event: ipy.events.EntitlementDelete) -> None:
        entitlement = event.entitlement

        if (
            str(entitlement.sku_id) != os.environ.get("PREMIUM_SKU_ID")
            or not entitlement.subscription_id
            or not entitlement._guild_id
        ):
            return

        await models.PremiumCode.prisma().delete_many(
            where={"customer_id": str(entitlement.subscription_id)}
        )


def setup(bot: utils.RealmBotBase) -> None:
    importlib.reload(utils)
    importlib.reload(cclasses)
    importlib.reload(pl_utils)
    importlib.reload(premium_utils)
    PremiumHandling(bot)
