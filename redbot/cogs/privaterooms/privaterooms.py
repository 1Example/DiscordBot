import contextlib
import logging
import re

import discord
from redbot.core import bank, commands, Config
from redbot.core.bot import Red
from typing import Optional
from .dashboard_integration import DashboardIntegration

log = logging.getLogger("red.privaterooms")


# Action keys, their default (unicode) emoji, and the panel description line.
# Guilds can override any emoji with one of their own; nothing here is tied to
# a specific server.
# What each button looks like out of the box. Guilds override the emoji, the
# label and the colour from the dashboard, so nothing here is fixed.
BUTTON_STYLES = {
    "secondary": discord.ButtonStyle.secondary,
    "primary": discord.ButtonStyle.primary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

# Mostly transparent: "secondary" is the only Discord style that takes no
# colour of its own, so the icon does the talking rather than the fill. Two
# exceptions - Kick throws somebody out and Claim hands over ownership, so
# neither should look like the toggle sitting next to it. Every one of these
# is still overridable from the dashboard.
DEFAULT_STYLES = {
    "lock": "secondary",
    "unlock": "secondary",
    "hide": "secondary",
    "unhide": "secondary",
    "rename": "secondary",
    "limit": "secondary",
    "kick": "danger",
    "claim": "success",
}

ACTIONS = (
    ("lock", "\N{LOCK}", "Lock", "stop new people from joining"),
    ("unlock", "\N{OPEN LOCK}", "Unlock", "let anyone join again"),
    ("hide", "\N{GHOST}", "Hide", "hide the room from the channel list"),
    ("unhide", "\N{EYE}", "Unhide", "make the room visible again"),
    ("rename", "\N{PENCIL}", "Rename", "change your room's name"),
    ("limit", "\N{BUST IN SILHOUETTE}", "Limit", "set a max number of people (0 = unlimited)"),
    ("kick", "\N{WOMANS BOOTS}", "Kick", "remove someone from your room"),
    ("claim", "\N{CROWN}", "Claim", "take ownership of an empty-of-owner room"),
)

# The panel's two button rows, and the two columns of the legend above them.
# Reading down a column tells you what the row of buttons under it does.
ACTION_GROUPS = (
    ("Access", ("lock", "unlock", "hide", "unhide")),
    ("Manage", ("rename", "limit", "kick", "claim")),
)

# An inline embed field is only about half the card wide, so the sentences in
# ACTIONS - written for the dashboard, where there is room for them - wrap onto
# three lines each and turn the legend into a wall of text. These are the same
# meanings, cut to something that fits one line in a column.
PANEL_BLURBS = {
    "lock": "no new joins",
    "unlock": "anyone can join",
    "hide": "hide from the list",
    "unhide": "show it again",
    "rename": "change the name",
    "limit": "cap the headcount",
    "kick": "remove someone",
    "claim": "take an ownerless room",
}

DEFAULT_EMOJIS = {key: default for key, default, _label, _blurb in ACTIONS}
DEFAULT_EMOJIS["hub"] = "\N{SPEAKER WITH THREE SOUND WAVES}"
DEFAULT_EMOJIS["public"] = "\N{SPEAKER WITH THREE SOUND WAVES}"
DEFAULT_EMOJIS["private"] = "\N{LOCK}"

CUSTOM_EMOJI_RE = re.compile(r"^<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>$")


def parse_emoji(raw: Optional[str]):
    """Turn a stored emoji string into something discord.py accepts.

    Accepts a unicode emoji or a full `<:name:id>` / `<a:name:id>` token.
    Returns None when the value is unusable so a bad override degrades to no
    emoji rather than breaking the whole panel.
    """
    if not raw:
        return None
    raw = raw.strip()
    match = CUSTOM_EMOJI_RE.match(raw)
    if match:
        animated, name, emoji_id = match.groups()
        return discord.PartialEmoji(name=name, id=int(emoji_id), animated=bool(animated))
    # Anything short and non-ASCII is treated as a unicode emoji.
    if len(raw) <= 8 and not raw.isascii():
        return raw
    return None


def resolve_emojis(overrides: Optional[dict]) -> dict:
    merged = dict(DEFAULT_EMOJIS)
    for key, value in (overrides or {}).items():
        if value:
            merged[key] = value
    return merged


def room_embed(guild: discord.Guild, settings: Optional[dict] = None) -> discord.Embed:
    """Build the hub panel embed for this guild.

    Title, colour and every emoji come from config, defaulting to the guild's
    own name so a fresh install reads correctly with no setup.
    """
    settings = settings or {}
    emojis = resolve_emojis(settings.get("emojis"))
    labels = settings.get("button_labels") or {}
    title = settings.get("panel_title") or f"{guild.name} | Voice Hub"
    public_name = settings.get("hub_public_name") or "CREATE PUBLIC"
    private_name = settings.get("hub_private_name") or "CREATE PRIVATE"

    # Mention the real channel where we can - a clickable hub beats a name the
    # member then has to go hunting for in the sidebar.
    def hub_label(config_key: str) -> str:
        # The field name already carries the hub's name, so an unset hub says
        # so quietly rather than repeating the name back at you.
        channel = guild.get_channel(settings.get(config_key) or 0)
        if channel is None:
            return "-# *not set up yet*"
        return f"\N{DOWNWARDS ARROW WITH TIP RIGHTWARDS} {channel.mention}"

    colour_value = settings.get("panel_colour")
    try:
        colour = discord.Colour(int(colour_value)) if colour_value else discord.Colour.blurple()
    except (TypeError, ValueError):
        colour = discord.Colour.blurple()

    embed = discord.Embed(
        title=f"{emojis['hub']} {title}",
        description=(
            "Join a hub below and a room is made for you straight away. "
            "You will be pinged here once it exists."
        ),
        colour=colour,
    )
    # The guild icon fills the empty right-hand side of the header, which is
    # otherwise dead space above the two hub columns.
    if guild.icon is not None:
        embed.set_thumbnail(url=guild.icon.url)

    # Pricing is only mentioned when there is something to pay, so a free
    # server's panel stays uncluttered.
    cost_public = cost_private = 0
    if settings.get("economy_enabled"):
        cost_public = int(settings.get("cost_public", 0) or 0)
        cost_private = int(settings.get("cost_private", 0) or 0)
    currency = settings.get("currency_name") or "credits"

    def price_suffix(amount: int) -> str:
        return f"\n\N{MONEY BAG} **{amount}** {currency}" if amount else ""

    # What the hub *does* comes first and the channel to join second. The
    # sentence is the same shape on every server, so the two columns stay
    # aligned even when one hub is set up and the other is not.
    def hub_value(config_key: str, blurb: str, amount: int) -> str:
        return f"{blurb}\n{hub_label(config_key)}{price_suffix(amount)}"

    embed.add_field(
        name=f"{emojis['public']} {public_name}",
        value=hub_value("hub_public", "Anyone can join.", cost_public),
        inline=True,
    )
    embed.add_field(
        name=f"{emojis['private']} {private_name}",
        value=hub_value("hub_private", "Locked until you invite people.", cost_private),
        inline=True,
    )

    # A full-width field forces a line break, so the legend below starts on a
    # row of its own instead of being packed in beside the hubs.
    embed.add_field(
        name="\N{DOWNWARDS BLACK ARROW} Your room",
        value=(
            "Own a room and the buttons below manage it. **Claim** is the only "
            "one that works on a room you do not own yet."
        ),
        inline=False,
    )

    # One column per row of buttons, in button order, so the legend reads as a
    # key to the two rows sitting directly underneath it.
    blurbs = {key: blurb for key, _default, _label, blurb in ACTIONS}
    fallback_labels = {key: label for key, _default, label, _blurb in ACTIONS}
    for heading, keys in ACTION_GROUPS:
        embed.add_field(
            name=heading,
            value="\n".join(
                f"{emojis[key]} **{labels.get(key) or fallback_labels[key]}**"
                f" \N{EN DASH} {PANEL_BLURBS.get(key) or blurbs[key]}"
                for key in keys
            ),
            inline=True,
        )

    footer = settings.get("panel_footer") or (
        f"Rooms run at this server's maximum voice quality "
        f"\N{EM DASH} {guild.bitrate_limit // 1000} kbps."
    )
    embed.set_footer(text=footer)
    return embed


class RenameModal(discord.ui.Modal, title="Rename your room"):
    name = discord.ui.TextInput(
        label="New room name", max_length=95, min_length=1, required=True
    )

    def __init__(self, cog: "PrivateRooms", channel: discord.VoiceChannel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.channel.edit(name=str(self.name), reason=f"Renamed by {interaction.user}")
            await interaction.response.send_message(
                f"Room renamed to **{self.name}**.", ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't rename the room: {e}", ephemeral=True)


class LimitModal(discord.ui.Modal, title="Set user limit"):
    limit = discord.ui.TextInput(
        label="Max users (0 = unlimited, max 99)", max_length=2, min_length=1, required=True
    )

    def __init__(self, cog: "PrivateRooms", channel: discord.VoiceChannel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.limit).strip()
        if not raw.isdigit():
            await interaction.response.send_message("That's not a valid number.", ephemeral=True)
            return
        value = int(raw)
        if value > 99:
            value = 99
        try:
            await self.channel.edit(user_limit=value, reason=f"Limit set by {interaction.user}")
            shown = "unlimited" if value == 0 else str(value)
            await interaction.response.send_message(f"User limit set to **{shown}**.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't set the limit: {e}", ephemeral=True)


class KickSelectView(discord.ui.View):
    def __init__(self, cog: "PrivateRooms", channel: discord.VoiceChannel):
        super().__init__(timeout=60)
        self.cog = cog
        self.channel = channel
        self.add_item(self.KickSelect(channel))

    class KickSelect(discord.ui.UserSelect):
        def __init__(self, channel: discord.VoiceChannel):
            super().__init__(placeholder="Choose a member to remove from your room…", min_values=1, max_values=1)
            self.channel = channel

        async def callback(self, interaction: discord.Interaction):
            member = self.values[0]
            if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel != self.channel:
                await interaction.response.send_message("That member isn't in your room.", ephemeral=True)
                return
            try:
                await member.move_to(None, reason=f"Kicked by room owner {interaction.user}")
                await interaction.response.send_message(f"Removed **{member.display_name}** from your room.", ephemeral=True)
            except discord.HTTPException as e:
                await interaction.response.send_message(f"Couldn't remove them: {e}", ephemeral=True)


class ControlPanelView(discord.ui.View):
    """A single, persistent view. One instance is registered globally in
    cog_load and works for every guild/message it's attached to, since every
    callback resolves the acting member's room dynamically."""

    def __init__(
        self,
        cog: "PrivateRooms",
        emojis: Optional[dict] = None,
        labels: Optional[dict] = None,
        styles: Optional[dict] = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        # discord.py matches persistent views by custom_id, so the globally
        # registered instance needs no styling. The copy used when *posting* a
        # panel carries the guild's own, and those are what the message keeps.
        for child in self.children:
            if not isinstance(child, discord.ui.Button) or not child.custom_id:
                continue
            key = child.custom_id.split(":", 1)[-1]
            if emojis:
                parsed = parse_emoji(emojis.get(key))
                if parsed is not None:
                    child.emoji = parsed
            if labels and (label := (labels.get(key) or "").strip()):
                child.label = label[:80]
            wanted = (styles or {}).get(key) or DEFAULT_STYLES.get(key)
            if wanted in BUTTON_STYLES:
                child.style = BUTTON_STYLES[wanted]

    async def _get_channel_or_warn(self, interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
        channel = await self.cog.get_owned_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "You don't currently own a room. Join **CREATE PUBLIC** or **CREATE PRIVATE** to create one.",
                ephemeral=True,
            )
        return channel

    @discord.ui.button(label="Lock", style=discord.ButtonStyle.secondary, custom_id="prooms:lock", row=0)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.connect = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Locked by {interaction.user}")
        await interaction.response.send_message("Room locked. Only existing members and anyone you allow can join.", ephemeral=True)

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.secondary, custom_id="prooms:unlock", row=0)
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.connect = None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")
        await interaction.response.send_message("Room unlocked. Anyone can join now.", ephemeral=True)

    @discord.ui.button(label="Hide", style=discord.ButtonStyle.secondary, custom_id="prooms:hide", row=0)
    async def hide(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.view_channel = False
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Hidden by {interaction.user}")
        await interaction.response.send_message("Room hidden from the channel list.", ephemeral=True)

    @discord.ui.button(label="Unhide", style=discord.ButtonStyle.secondary, custom_id="prooms:unhide", row=0)
    async def unhide(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.view_channel = None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=f"Unhidden by {interaction.user}")
        await interaction.response.send_message("Room visible again.", ephemeral=True)

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.secondary, custom_id="prooms:rename", row=1)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self.cog.get_owned_channel(interaction)
        if channel is None:
            await interaction.response.send_message("You don't currently own a room.", ephemeral=True)
            return
        await interaction.response.send_modal(RenameModal(self.cog, channel))

    @discord.ui.button(label="Limit", style=discord.ButtonStyle.secondary, custom_id="prooms:limit", row=1)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self.cog.get_owned_channel(interaction)
        if channel is None:
            await interaction.response.send_message("You don't currently own a room.", ephemeral=True)
            return
        await interaction.response.send_modal(LimitModal(self.cog, channel))

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.secondary, custom_id="prooms:kick", row=1)
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await self._get_channel_or_warn(interaction)
        if channel is None:
            return
        if len(channel.members) <= 1:
            await interaction.response.send_message("There's nobody else in your room to remove.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choose who to remove:", view=KickSelectView(self.cog, channel), ephemeral=True
        )

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, custom_id="prooms:claim", row=1)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("You need to be in the room you want to claim.", ephemeral=True)
            return
        channel = member.voice.channel
        rooms = await self.cog.config.guild(interaction.guild).rooms()
        data = rooms.get(str(channel.id))
        if data is None:
            await interaction.response.send_message("This isn't a private room.", ephemeral=True)
            return
        owner_id = data.get("owner")
        owner_still_here = any(m.id == owner_id for m in channel.members)
        if owner_still_here and owner_id != member.id:
            await interaction.response.send_message("The current owner is still in the room.", ephemeral=True)
            return
        await self.cog.set_owner(interaction.guild, channel, member)
        await interaction.response.send_message("You are now the owner of this room.", ephemeral=True)


class PrivateRooms(DashboardIntegration, commands.Cog):
    """Join-to-create voice rooms (public or private) with a shared control panel."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=847362910457, force_registration=True)
        default_guild = {
            "hub_private": None,
            "hub_public": None,
            "category": None,
            "panel_channel": None,
            "panel_message": None,
            "default_limit": 0,
            "name_format_private": "{user}'s Room",
            "name_format_public": "{user}'s Room",
            # Presentation, all overridable per guild - nothing server-specific
            # is baked into the code.
            "panel_title": None,
            "panel_footer": None,
            "panel_colour": None,
            "hub_public_name": "CREATE PUBLIC",
            "hub_private_name": "CREATE PRIVATE",
            "emojis": {},
            "button_labels": {},
            # Tokens for emoji this cog uploaded itself, so a replaced image can
            # be deleted instead of sitting in the bot's emoji list forever.
            "owned_emojis": {},
            "button_styles": {},
            # Creating a room can cost credits. 0 is free; the two kinds are
            # priced separately so a private room can be worth more.
            "economy_enabled": False,
            "cost_public": 0,
            "cost_private": 0,
            # After a room is made, point its owner at the panel. Discord has no
            # way to send a truly private message outside an interaction, so
            # this is a mention that removes itself.
            "notice_enabled": True,
            "notice_text": (
                "{user}, your room is ready \N{EM DASH} from here you can control "
                "your voice chat."
            ),
            "notice_delete_after": 30,
            "rooms": {},  # str(voice_channel_id) -> {"owner": user_id, "public": bool}
        }
        self.config.register_guild(**default_guild)
        self._panel_view_added = False

    async def cog_load(self):
        if not self._panel_view_added:
            self.bot.add_view(ControlPanelView(self))
            self._panel_view_added = True

    # ---------- helpers ----------

    async def get_owned_channel(self, interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
        guild = interaction.guild
        if guild is None:
            return None
        rooms = await self.config.guild(guild).rooms()
        for channel_id_str, data in rooms.items():
            if data.get("owner") == interaction.user.id:
                channel = guild.get_channel(int(channel_id_str))
                if isinstance(channel, discord.VoiceChannel):
                    return channel
        return None

    async def set_owner(self, guild: discord.Guild, channel: discord.VoiceChannel, member: discord.Member):
        async with self.config.guild(guild).rooms() as rooms:
            entry = rooms.get(str(channel.id), {})
            old_owner_id = entry.get("owner")
            entry["owner"] = member.id
            rooms[str(channel.id)] = entry
        try:
            if old_owner_id and old_owner_id != member.id:
                old_owner = guild.get_member(old_owner_id)
                if old_owner:
                    await channel.set_permissions(old_owner, overwrite=None, reason="Ownership transferred")
            await channel.set_permissions(
                member,
                connect=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
                reason="New room owner",
            )
        except discord.HTTPException:
            pass

    async def _charge_for_room(
        self, member: discord.Member, settings: dict, public: bool
    ) -> tuple[bool, int, str]:
        """Bill the member for a room. Returns (allowed, cost, currency)."""
        if not settings.get("economy_enabled"):
            return True, 0, ""
        cost = int(settings.get("cost_public" if public else "cost_private", 0) or 0)
        if cost <= 0:
            return True, 0, ""
        try:
            currency = await bank.get_currency_name(member.guild)
            if not await bank.can_spend(member, cost):
                return False, cost, currency
            await bank.withdraw_credits(member, cost)
        except Exception:  # noqa: BLE001 - a broken bank should not block rooms
            return True, 0, ""
        return True, cost, currency

    async def _send_notice(self, guild: discord.Guild, settings: dict, text: str) -> None:
        """Post a self-removing message in the panel channel."""
        channel = guild.get_channel(settings.get("panel_channel") or 0)
        me = guild.me
        if channel is None or me is None or not hasattr(channel, "send"):
            return
        if not channel.permissions_for(me).send_messages:
            return
        delete_after = settings.get("notice_delete_after") or 0
        try:
            await channel.send(
                text,
                delete_after=delete_after or None,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            pass

    async def create_room(self, member: discord.Member, public: bool):
        guild = member.guild
        settings = await self.config.guild(guild).all()

        allowed, cost, currency = await self._charge_for_room(member, settings, public)
        if not allowed:
            # Nothing was taken, so put them back where they came from rather
            # than leaving them sitting in the hub.
            with contextlib.suppress(discord.HTTPException):
                await member.move_to(None, reason="Cannot afford a room")
            await self._send_notice(
                guild,
                settings,
                f"{member.mention} a room costs {cost} {currency}, "
                f"which is more than you have.",
            )
            return
        category = guild.get_channel(settings["category"]) if settings["category"] else None
        hub_id = settings["hub_public"] if public else settings["hub_private"]
        hub = guild.get_channel(hub_id) if hub_id else None

        name_format = settings["name_format_public"] if public else settings["name_format_private"]
        name = name_format.format(user=member.display_name)[:95]

        # Rooms are always created at the maximum bitrate the server's boost
        # level allows (e.g. up to 384 kbps on Tier 3).
        bitrate = guild.bitrate_limit

        everyone_overwrite = discord.PermissionOverwrite(
            connect=True if public else False,
            view_channel=True,
        )
        overwrites = {
            guild.default_role: everyone_overwrite,
            member: discord.PermissionOverwrite(
                connect=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
            ),
        }

        try:
            channel = await guild.create_voice_channel(
                name=name,
                category=category if isinstance(category, discord.CategoryChannel) else (hub.category if hub else None),
                bitrate=bitrate,
                user_limit=settings["default_limit"],
                overwrites=overwrites,
                reason=f"{'Public' if public else 'Private'} room created for {member}",
            )
        except discord.HTTPException:
            # They paid for a room Discord then refused to make.
            if cost:
                with contextlib.suppress(Exception):
                    await bank.deposit_credits(member, cost)
            return

        async with self.config.guild(guild).rooms() as rooms:
            rooms[str(channel.id)] = {"owner": member.id, "public": public}

        try:
            await member.move_to(channel, reason="Moved to their new room")
        except discord.HTTPException:
            pass

        if settings.get("notice_enabled"):
            template = settings.get("notice_text") or ""
            try:
                text = template.format(
                    user=member.mention,
                    room=channel.mention,
                    cost=cost,
                    currency=currency,
                )
            except (KeyError, IndexError, ValueError):
                # A typo in the template is not worth losing the pointer over.
                text = f"{member.mention} from here you can control your voice chat."
            # Only tack the price on when the template did not already say it.
            if cost and "{cost}" not in template:
                text = f"{text} It cost you {cost} {currency}."
            await self._send_notice(guild, settings, text)

    async def maybe_delete_room(self, channel: discord.VoiceChannel):
        guild = channel.guild
        rooms = await self.config.guild(guild).rooms()
        if str(channel.id) not in rooms:
            return
        if len(channel.members) > 0:
            return
        try:
            await channel.delete(reason="Room empty")
        except discord.HTTPException:
            # Forgetting the room before the delete succeeds strands the channel:
            # nothing tracks it any more, so nothing will ever try again. Keep
            # the entry so the next person to leave, or the dashboard cleanup,
            # gets another go at it.
            log.warning(
                "Could not delete the empty room %s (%s) in %s; leaving it tracked "
                "so it can be retried.",
                channel.name,
                channel.id,
                guild.name,
            )
            return
        async with self.config.guild(guild).rooms() as rooms:
            rooms.pop(str(channel.id), None)

    # ---------- listener ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        settings = await self.config.guild(guild).all()
        hub_private_id = settings["hub_private"]
        hub_public_id = settings["hub_public"]

        if after.channel is not None:
            if hub_private_id is not None and after.channel.id == hub_private_id:
                await self.create_room(member, public=False)
            elif hub_public_id is not None and after.channel.id == hub_public_id:
                await self.create_room(member, public=True)

        if before.channel is not None and str(before.channel.id) in settings["rooms"]:
            await self.maybe_delete_room(before.channel)

    # ---------- commands ----------
