import os
import typing

import discord
from AAA3A_utils import CogsUtils
from redbot.core import commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import humanize_list

from .converters import ListStringToEmbed
from .inline_editor import build_fragment

_: Translator = Translator("EmbedUtils", __file__)


def dashboard_page(*args, **kwargs):
    def decorator(func: typing.Callable):
        func.__dashboard_decorator_params__ = (args, kwargs)
        return func

    return decorator


# `?raw=1` still serves the original standalone document. Nothing links to it,
# but it is the untouched page, which makes it the thing to compare against if
# the inlined build ever looks wrong.
RAW_QUERY_KEY = "raw"

# Built once: the transform walks ~91KB of CSS, and the result is the same for
# every request.
_FRAGMENT: str | None = None


def _editor_fragment() -> str:
    global _FRAGMENT
    if _FRAGMENT is None:
        _FRAGMENT = build_fragment()
    return _FRAGMENT


def _wants_raw(kwargs) -> bool:
    """True when the request asked for the original standalone document."""
    value = (kwargs.get("extra_kwargs") or {}).get(RAW_QUERY_KEY)
    if isinstance(value, (list, tuple)):
        value = value[-1] if value else None
    return str(value) in ("1", "true", "yes")


def _raw_document() -> str:
    file_path = os.path.join(os.path.dirname(__file__), "editor.html")
    with open(file_path, encoding="utf-8") as f:
        return f.read()


class DashboardIntegration:
    bot: Red

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog: commands.Cog) -> None:
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(name=None, description="Create rich Embeds!")
    async def dashboard_editor(self, **kwargs) -> None:
        if _wants_raw(kwargs):
            return {"status": 0, "web_content": {"source": _raw_document(), "standalone": True}}
        # A normal module page: the editor renders inside the dashboard shell,
        # with the topbar, breadcrumb and back button around it.
        return {"status": 0, "web_content": {"source": _editor_fragment()}}

    # The page slug is what the Modules card labels its button with, and
    # "Guild" described the scope rather than what the page does. "Create" is
    # the verb; the guild it applies to is already named in the breadcrumb.
    @dashboard_page(
        name="create",
        description="Build an embed and send it to a channel in this server.",
        methods=("GET", "POST"),
        # Stated rather than inferred. This handler takes `member`, so the
        # decorator adds `member_id` to its context ids, and the auto-hide rule
        # hides any page needing a context id beyond user/guild - on the
        # assumption the dashboard cannot supply one. It can: `member` is
        # resolved from the signed-in user plus the guild being viewed, both of
        # which the Modules card already has. Left to infer, this page exists
        # but never gets a button.
        hidden=False,
    )
    async def dashboard_guild(self, member: discord.Member, guild: discord.Guild, **kwargs) -> None:
        is_owner = member.id in self.bot.owner_ids
        if (
            not is_owner
            and not await self.bot.is_mod(member)
            and not member.guild_permissions.manage_guild
        ):
            return {
                "status": 0,
                "error_code": 403,
                "message": _("You don't have permissions to access this page."),
            }
        channels = kwargs["get_sorted_channels"](guild)
        if not channels:
            return {
                "status": 0,
                "error_code": 403,
                "message": _(
                    "I or you don't have permissions to send messages or embeds in any channel in this guild.",
                ),
            }

        # The send form is built either way: on the inlined page it is rendered
        # into the editor's own sending area, exactly where the standalone
        # document put it.
        raw = _wants_raw(kwargs)

        import wtforms

        class SendForm(kwargs["Form"]):
            def __init__(self) -> None:
                super().__init__(prefix="send_form_")

            username: wtforms.HiddenField = wtforms.HiddenField(
                _("Username:"),
                validators=[wtforms.validators.Optional(), wtforms.validators.Length(max=80)],
            )
            avatar: wtforms.HiddenField = wtforms.HiddenField(
                _("Avatar URL:"),
                validators=[wtforms.validators.Optional(), wtforms.validators.URL()],
            )
            data: wtforms.HiddenField = wtforms.HiddenField(
                _("Data"),
                validators=[
                    wtforms.validators.DataRequired(),
                    kwargs["DpyObjectConverter"](ListStringToEmbed),
                ],
            )
            channels: wtforms.SelectMultipleField = wtforms.SelectMultipleField(
                _("Channels:"),
                choices=[],
                validators=[
                    wtforms.validators.DataRequired(),
                    kwargs["DpyObjectConverter"](
                        typing.Union[discord.TextChannel, discord.VoiceChannel],
                    ),
                ],
            )
            submit = wtforms.SubmitField(_("Send Message(s)"))

        send_form: SendForm = SendForm()
        send_form.channels.choices = channels
        send_form_string = f"""
            <form action="" method="POST" role="form" enctype="multipart/form-data">
                {send_form.hidden_tag()}
                {send_form.channels()}
                {send_form.submit(onclick='this.parentElement.querySelector("#send_form_username").value = document.querySelector(".editSenderUsername").value; this.parentElement.querySelector("#send_form_avatar").value = document.querySelector(".editSenderAvatar").value; this.parentElement.querySelector("#send_form_data").value = (JSON.stringify(typeof jsonCode === "object" ? jsonCode : json));', style="cursor: pointer; margin-left: 105px;")}
            </form>
        """

        if send_form.validate_on_submit() and await send_form.validate_dpy_converters():
            notifications = []
            for channel in send_form.channels.data:
                if send_form.username.data or send_form.avatar.data:
                    if not channel.permissions_for(guild.me).manage_webhooks:
                        notifications.append(
                            {
                                "message": f"{channel.name} ({channel.id}): I don't have permissions to manage webhooks in this channel.",
                                "category": "danger",
                            },
                        )
                        continue
                    if not is_owner and not channel.permissions_for(member).manage_webhooks:
                        notifications.append(
                            {
                                "message": f"{channel.name} ({channel.id}): You don't have permissions to manage webhooks in this channel.",
                                "category": "danger",
                            },
                        )
                        continue
                    try:
                        hook: discord.Webhook = await CogsUtils.get_hook(
                            bot=self.bot,
                            channel=channel,
                        )
                        await hook.send(
                            **send_form.data.data,
                            username=send_form.username.data or guild.me.display_name,
                            avatar_url=send_form.avatar.data or guild.me.display_avatar,
                            wait=True,
                        )
                    except discord.HTTPException as error:
                        notifications.append(
                            {
                                "message": f"{channel.name} ({channel.id}): {str(error)}",
                                "category": "danger",
                            },
                        )
                else:
                    try:
                        await channel.send(**send_form.data.data)
                    except (ValueError, TypeError, discord.HTTPException) as e:
                        notifications.append(
                            {
                                "message": f"{channel.name} ({channel.id}): {str(e)}",
                                "category": "danger",
                            },
                        )
            s = "s" if len(send_form.channels.data) > 1 else ""
            self.logger.trace(
                f"{len(send_form.channels.data)} message{s} sent in {humanize_list([f'`#{channel.name}` ({channel.id})' for channel in send_form.channels.data])} in `{guild.name}` ({guild.id}), from the Dashboard by `{member.display_name}` ({member.id}).",
            )
            if not notifications:
                notifications.append(
                    {
                        "message": _("Message{s} sent successfully!").format(s=s),
                        "category": "success",
                    },
                )
            return {
                "status": 0,
                "notifications": notifications,
                "redirect_url": kwargs["request_url"],
            }

        if raw:
            return {
                "status": 0,
                "web_content": {
                    "source": _raw_document(),
                    "standalone": True,
                    "send_form": send_form_string,
                },
            }
        return {
            "status": 0,
            "web_content": {"source": _editor_fragment(), "send_form": send_form_string},
        }

    # `guild` was this page's slug before it was renamed to `create`. Old links
    # and bookmarks (and a Modules card cached in someone's browser) still
    # point at it, and after the rename that slug would simply not exist. It is
    # kept as an alias onto the same handler so those URLs land on the inlined
    # editor rather than nowhere. Hidden, so the Modules card shows one button.
    @dashboard_page(
        name="guild",
        description="Build an embed and send it to a channel in this server.",
        methods=("GET", "POST"),
        hidden=True,
    )
    async def dashboard_guild_alias(
        self, member: discord.Member, guild: discord.Guild, **kwargs
    ) -> None:
        return await self.dashboard_guild(member=member, guild=guild, **kwargs)
