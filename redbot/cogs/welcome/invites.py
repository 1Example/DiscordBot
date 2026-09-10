"""Pay people for bringing someone in, and pay the someone for arriving.

Discord does not say which invite a member used, so this does what every
invite tracker does: keep the use count of every invite, and when someone
joins, look for the one that went up. Ambiguity is treated as not knowing -
if two counts moved between snapshots, nobody is credited, because guessing
would pay the wrong person.

Needs Manage Server to read invites at all. Without it the rest of the cog
carries on and this quietly does nothing.
"""
from __future__ import annotations

import discord
from red_commons.logging import getLogger
from redbot.core import bank, commands, errors
from redbot.core.i18n import Translator

log = getLogger("red.trusty-cogs.Welcome")
_ = Translator("Welcome", __file__)

INVITE_DEFAULTS = {
    # Both 0 means no rewards, which is how this cog behaved before.
    "INVITE_REWARD_INVITER": 0,
    "INVITE_REWARD_JOINER": 0,
    # Accounts younger than this are never rewarded, nor is whoever brought
    # them: a day-old account is the cheapest thing on the internet to make.
    "INVITE_REWARD_MIN_AGE_DAYS": 7,
    # Pay for a given person once, so leaving and rejoining is not a wage.
    "INVITE_REWARD_ONCE": True,
    "INVITE_REWARDED": [],
}


class InviteRewards:
    """Invite tracking and the payouts that hang off it."""

    # Built on first use rather than in __init__. A mixin cannot rely on the
    # cog it is mixed into calling super().__init__() - Welcome does not - and
    # a mixin that only works when the host remembers to is a trap.

    @property
    def invite_uses(self) -> dict[int, dict[str, int]]:
        """guild id -> {invite code: uses}."""
        cache = getattr(self, "_invite_uses", None)
        if cache is None:
            cache = {}
            self._invite_uses = cache
        return cache

    @property
    def _invite_warned(self) -> set[int]:
        """Guilds already logged about as unreadable, so it is said once."""
        seen = getattr(self, "_invite_warned_ids", None)
        if seen is None:
            seen = set()
            self._invite_warned_ids = seen
        return seen

    # ---------------- keeping the counts ----------------

    async def _fetch_invite_uses(self, guild: discord.Guild) -> dict[str, int] | None:
        """Every invite's use count, or None when we are not allowed to look."""
        me = guild.me
        if me is None or not me.guild_permissions.manage_guild:
            if guild.id not in self._invite_warned:
                self._invite_warned.add(guild.id)
                log.info(
                    "No Manage Server in %s, so invite rewards are off there.", guild.id
                )
            return None
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException) as e:
            log.debug("Could not read invites in %s: %s", guild.id, e)
            return None
        uses = {invite.code: invite.uses or 0 for invite in invites}
        if "VANITY_URL" in guild.features:
            try:
                vanity = await guild.vanity_invite()
            except (discord.Forbidden, discord.HTTPException):
                vanity = None
            if vanity is not None and vanity.code:
                uses[vanity.code] = vanity.uses or 0
        return uses

    async def refresh_invite_cache(self, guild: discord.Guild) -> None:
        """Snapshot a guild's invites, if it has rewards switched on."""
        settings = await self.config.guild(guild).all()
        if not settings.get("INVITE_REWARD_INVITER") and not settings.get(
            "INVITE_REWARD_JOINER"
        ):
            self.invite_uses.pop(guild.id, None)
            return
        uses = await self._fetch_invite_uses(guild)
        if uses is not None:
            self.invite_uses[guild.id] = uses

    async def refresh_all_invite_caches(self) -> None:
        for guild in self.bot.guilds:
            await self.refresh_invite_cache(guild)

    async def _find_used_invite(self, guild: discord.Guild) -> discord.Invite | None:
        """The invite whose count went up since the last snapshot.

        None when we cannot tell: no cache yet, no permission, nothing moved,
        or more than one moved. Paying on a guess is worse than not paying.
        """
        previous = self.invite_uses.get(guild.id)
        me = guild.me
        if me is None or not me.guild_permissions.manage_guild:
            return None
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return None
        current = {invite.code: invite for invite in invites}
        self.invite_uses[guild.id] = {
            code: invite.uses or 0 for code, invite in current.items()
        }
        if previous is None:
            return None
        grew = [
            invite
            for code, invite in current.items()
            if (invite.uses or 0) > previous.get(code, 0)
        ]
        if len(grew) != 1:
            # Either nothing we can see changed - a vanity url, a one-use
            # invite that vanished as it was used - or two people arrived at
            # once and there is no way to tell which is which.
            return None
        return grew[0]

    # ---------------- paying ----------------

    async def _pay(self, member: discord.Member, amount: int) -> bool:
        if amount <= 0:
            return False
        try:
            await bank.deposit_credits(member, amount)
        except errors.BalanceTooHigh as e:
            await bank.set_balance(member, e.max_balance)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not pay %s an invite reward: %s", member, e)
            return False
        return True

    async def handle_invite_rewards(self, member: discord.Member) -> None:
        """Pay the new member and whoever brought them, where that is known."""
        guild = member.guild
        if member.bot:
            return
        settings = await self.config.guild(guild).all()
        joiner_reward = int(settings.get("INVITE_REWARD_JOINER") or 0)
        inviter_reward = int(settings.get("INVITE_REWARD_INVITER") or 0)
        if not joiner_reward and not inviter_reward:
            return

        min_age = int(settings.get("INVITE_REWARD_MIN_AGE_DAYS") or 0)
        if min_age:
            age = discord.utils.utcnow() - member.created_at
            if age.days < min_age:
                log.debug(
                    "%s is %s days old, under the %s day minimum for invite rewards.",
                    member,
                    age.days,
                    min_age,
                )
                return

        if settings.get("INVITE_REWARD_ONCE", True):
            rewarded = settings.get("INVITE_REWARDED") or []
            if member.id in rewarded:
                log.debug("%s has been rewarded here before.", member)
                return

        invite = await self._find_used_invite(guild)
        inviter = invite.inviter if invite is not None else None
        # An invite made by someone who has since left, or by the bot itself,
        # pays nobody - but the person who arrived is still paid.
        inviter_member = guild.get_member(inviter.id) if inviter is not None else None
        if inviter_member is not None and (
            inviter_member.bot or inviter_member.id == member.id
        ):
            inviter_member = None

        paid_joiner = await self._pay(member, joiner_reward)
        paid_inviter = (
            await self._pay(inviter_member, inviter_reward)
            if inviter_member is not None
            else False
        )
        if not paid_joiner and not paid_inviter:
            return

        if settings.get("INVITE_REWARD_ONCE", True):
            async with self.config.guild(guild).INVITE_REWARDED() as rewarded:
                if member.id not in rewarded:
                    rewarded.append(member.id)

        currency = await bank.get_currency_name(guild)
        log.info(
            "Invite rewards in %s: %s got %s, %s got %s (%s)",
            guild.id,
            member,
            joiner_reward if paid_joiner else 0,
            inviter_member,
            inviter_reward if paid_inviter else 0,
            currency,
        )

    # ---------------- keeping the cache honest ----------------

    @commands.Cog.listener()
    async def on_guild_available(self, guild: discord.Guild) -> None:
        await self.refresh_invite_cache(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.refresh_invite_cache(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if not isinstance(guild, discord.Guild) or guild.id not in self.invite_uses:
            return
        self.invite_uses[guild.id][invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if not isinstance(guild, discord.Guild) or guild.id not in self.invite_uses:
            return
        self.invite_uses[guild.id].pop(invite.code, None)
