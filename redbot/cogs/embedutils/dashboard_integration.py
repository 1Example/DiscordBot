import os
import typing

import discord
from AAA3A_utils import CogsUtils
from redbot.core import commands
from redbot.core.bot import Red
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import humanize_list

from .converters import ListStringToEmbed

_: Translator = Translator("EmbedUtils", __file__)


def dashboard_page(*args, **kwargs):
    def decorator(func: typing.Callable):
        func.__dashboard_decorator_params__ = (args, kwargs)
        return func

    return decorator


# The editor is Glitchii's embed builder: a complete HTML document with ~2.5k
# lines of its own CSS written against `html`/`body` and a fixed two-pane
# viewport layout. Pasting that into a dashboard page would have the two
# stylesheets fighting in both directions, so it stays a standalone document
# and the dashboard page frames it. Same document either way - the wrapper only
# decides whether the shell (topbar, guild hero, tabs, back button) is around
# it.
#
# `?raw=1` on the page's own URL is what asks for the bare document, so both
# forms live on one registered page instead of showing an extra button in the
# Modules list that nobody should click directly.
RAW_QUERY_KEY = "raw"

FRAME_TEMPLATE = """
<style>
  .eu-frame-wrap { display:flex; flex-direction:column; gap:10px; }
  .eu-frame-bar  { display:flex; align-items:center; justify-content:flex-end; gap:9px;
                   font-size:.8rem; opacity:.7; }
  .eu-frame-bar a { color:inherit; text-decoration:none; display:inline-flex;
                    align-items:center; gap:6px; padding:5px 11px; border-radius:9px;
                    background:rgba(255,255,255,.05);
                    border:1px solid rgba(255,255,255,.10); }
  .eu-frame-bar a:hover { background:rgba(255,255,255,.10); color:inherit; }
  /* Tall enough to work in without being taller than the window; the editor
     scrolls internally, so the page itself never grows a second scrollbar. */
  .eu-frame { width:100%; height:min(1100px, calc(100vh - 210px)); min-height:620px;
              border:1px solid rgba(130,175,255,.16); border-radius:14px;
              background:#2f3136; display:block; }
</style>
<div class="eu-frame-wrap">
  <div class="eu-frame-bar">
    <a href="{{ raw_url }}" target="_blank" rel="noopener">
      <i class="fa fa-external-link"></i> Open in a new tab
    </a>
  </div>
  <iframe class="eu-frame" src="{{ raw_url }}" title="Embed editor"></iframe>
</div>
"""


def _wants_raw(kwargs) -> bool:
    """True when the request asked for the bare editor document."""
    value = (kwargs.get("extra_kwargs") or {}).get(RAW_QUERY_KEY)
    if isinstance(value, (list, tuple)):
        value = value[-1] if value else None
    return str(value) in ("1", "true", "yes")


def _raw_url(kwargs) -> str:
    """This page's own URL with `?raw=1` appended."""
    url = kwargs.get("request_url") or ""
    base = url.split("?", 1)[0]
    return f"{base}?{RAW_QUERY_KEY}=1"


def _frame(kwargs) -> dict:
    return {
        "status": 0,
        "web_content": {"source": FRAME_TEMPLATE, "raw_url": _raw_url(kwargs)},
    }


class DashboardIntegration:
    bot: Red

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog: commands.Cog) -> None:
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    @dashboard_page(name=None, description="Create rich Embeds!")
    async def dashboard_editor(self, **kwargs) -> None:
        if not _wants_raw(kwargs):
            return _frame(kwargs)
        file_path = os.path.join(os.path.dirname(__file__), "editor.html")
        with open(file_path, encoding="utf-8") as f:
            source = f.read()
        return {"status": 0, "web_content": {"source": source, "standalone": True}}

    @dashboard_page(
        name="guild",
        description="Create rich Embeds and send them to a guild!",
        methods=("GET", "POST"),
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

        # Both checks above run first, so a refusal is reported by the page
        # rather than by the framed document, where it would be a 403 rendered
        # inside a box on an otherwise normal-looking page.
        if not _wants_raw(kwargs):
            return _frame(kwargs)

        file_path = os.path.join(os.path.dirname(__file__), "editor.html")
        with open(file_path, encoding="utf-8") as f:
            source = f.read()

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

        return {
            "status": 0,
            "web_content": {"source": source, "standalone": True, "send_form": send_form_string},
        }
