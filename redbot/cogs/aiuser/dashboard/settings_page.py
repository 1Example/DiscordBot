"""The server settings page.

Every guild-scoped setting AIUser has, in one form. There are around fifty of
them, so the fields are described as data and the wtforms class is built from
that description rather than written out one declaration at a time - the same
information, a tenth of the code, and no way for a field to exist in the form
but not in the template or the save path.

The three prompt-shaped settings (`custom_text_prompt`, `random_messages_prompts`
and the preset bodies) are deliberately absent: they belong to the prompt page,
which renders them with token counts.
"""

import pathlib
import typing as t

import discord

from ..dashboard.decorator import dashboard_page
from ..types.abc import MixinMeta

TEMPLATES_PATH = pathlib.Path(__file__).parent / "templates"

# Values the cog stores but nobody sets by hand: caches of what a provider
# reported last time. Listing them would invite editing them.
STATE_KEYS = {
    "scan_audio_provider_history",
    "function_calling_voice_provider_history",
    "endpoint_model_history",
    "presets",
}

# (key, label, kind, help)
#   kind: bool | int | float | str | text | lines | channels | roles | members
#         | select:<choices-attr>
SECTIONS: tuple[tuple[str, str, tuple], ...] = (
    ("Replying", "fa-comment", (
        ("reply_percent", "Reply chance (%)", "float",
         "How often the bot answers a message it was not asked to."),
        ("reply_to_mentions_replies", "Always reply to mentions", "bool",
         "Answer whenever the bot is mentioned or replied to, whatever the chance above."),
        ("conversation_reply_percent", "Follow-up chance (%)", "int",
         "Chance of continuing a conversation it is already part of."),
        ("conversation_reply_time", "Follow-up window (seconds)", "int",
         "How long after its own message the bot still counts as in conversation."),
        ("always_reply_on_words", "Trigger words", "lines",
         "One per line. Any message containing one of these always gets a reply."),
        ("grok_trigger", "Respond to 'grok' style prompts", "bool",
         "Answer messages that address the bot the way people address Grok."),
        ("public_forget", "Anyone may clear context", "bool",
         "Let ordinary members reset the conversation, not just staff."),
        ("query_memories", "Use stored memories", "bool",
         "Let replies draw on remembered facts about the server."),
    )),
    ("Who and where", "fa-filter", (
        ("channels_whitelist", "Enabled channels", "channels",
         "Leave empty to allow every channel."),
        ("roles_whitelist", "Enabled roles", "roles",
         "Leave empty to allow every role."),
        ("members_whitelist", "Enabled members", "members",
         "Leave empty to allow every member."),
        ("optin_by_default", "Opt members in by default", "bool",
         "Treat members as consenting unless they opt out."),
        ("optin_disable_embed", "Hide the opt-in notice", "bool",
         "Stop showing the embed that explains how to opt in."),
        ("ignore_regex", "Ignore messages matching", "str",
         "A regular expression. Matching messages are never answered."),
        ("removelist_regexes", "Strip from replies", "lines",
         "One regular expression per line; matches are removed from the bot's reply."),
    )),
    ("Model", "fa-microchip", (
        ("model", "Model", "str",
         "The LLM used for replies in this server."),
        ("custom_model_tokens_limit", "Context limit override (tokens)", "int",
         "Only needed when the model's real limit is not known. 0 uses the default."),
        ("parameters", "Extra parameters (JSON)", "text",
         "Passed to the model as-is, for example temperature or top_p."),
        ("weights", "Token weights (JSON)", "text",
         "Per-token biases, for models that support them."),
    )),
    ("Context", "fa-layer-group", (
        ("messages_backread", "Messages of context", "int",
         "How many earlier messages are sent with each reply. Higher costs more."),
        ("messages_backread_seconds", "Context age limit (seconds)", "int",
         "Ignore earlier messages older than this."),
        ("messages_min_length", "Minimum message length", "int",
         "Shorter messages are not answered."),
        ("message_burst_idle_seconds", "Burst idle gap (seconds)", "int",
         "A pause this long ends a burst of messages and triggers a reply."),
        ("message_burst_max_seconds", "Burst maximum (seconds)", "int",
         "Reply after this long even if someone is still typing."),
        ("compaction_enabled", "Summarise long conversations", "bool",
         "Condense old context instead of dropping it."),
    )),
    ("Images", "fa-image", (
        ("scan_images", "Read images", "bool",
         "Let the bot look at images people post."),
        ("scan_images_model", "Vision model", "str",
         "Leave blank to use the reply model."),
        ("scan_images_detail", "Detail level", "str",
         "low, high, or auto."),
        ("max_image_size", "Maximum image size (bytes)", "int",
         "Larger images are skipped."),
    )),
    ("Audio", "fa-microphone", (
        ("scan_audio", "Transcribe audio", "bool",
         "Let the bot listen to voice messages and audio attachments."),
        ("scan_audio_provider", "Transcription provider", "str", ""),
        ("scan_audio_model", "Transcription model", "str",
         "Leave blank for the provider's default."),
        ("max_audio_duration", "Maximum duration (seconds)", "int",
         "Longer clips are skipped."),
    )),
    ("Random messages", "fa-dice", (
        ("random_messages_enabled", "Send unprompted messages", "bool",
         "Occasionally start a conversation with nobody having spoken."),
        ("random_messages_percent", "Chance (%)", "float",
         "How likely each opportunity is taken."),
    )),
    ("Tools", "fa-wrench", (
        ("function_calling", "Enable tools", "bool",
         "Let the model call functions - search, images, voice."),
        ("function_calling_functions", "Enabled tools", "lines",
         "One tool name per line. Empty enables all of them."),
        ("function_calling_tool_call_rounds", "Maximum tool rounds", "int",
         "How many times the model may call a tool before it must answer."),
        ("function_calling_search_provider", "Search provider", "str", ""),
        ("function_calling_search_endpoint", "Search endpoint", "str",
         "Only needed for a self-hosted search backend."),
        ("function_calling_search_max_results", "Search results", "int", ""),
        ("function_calling_scrape_provider", "Page-reading provider", "str", ""),
        ("function_calling_image_model", "Image generation model", "str", ""),
        ("function_calling_image_custom_endpoint", "Image endpoint", "str",
         "Only needed for a self-hosted image backend."),
        ("function_calling_voice_provider", "Voice provider", "str", ""),
        ("function_calling_voice_model", "Voice model", "str", ""),
        ("function_calling_voice", "Voice", "str",
         "The named voice to speak with."),
    )),
    ("Webhooks", "fa-plug", (
        ("reply_to_webhooks", "Reply to webhooks", "bool",
         "Answer messages posted by webhooks and other apps."),
        ("webhook_whitelist_enabled", "Restrict to listed webhooks", "bool",
         "Only answer the webhook users listed below."),
        ("webhook_user_whitelist", "Allowed webhook users", "lines",
         "One user ID per line."),
    )),
)

# Bot-wide, so only the owner sees them: an admin of one server editing these
# would be changing every other server's behaviour too.
OWNER_SECTION = ("Bot-wide (owner)", "fa-globe", (
    ("custom_openai_endpoint", "OpenAI-compatible endpoint", "str",
     "Point every server at another API. Blank uses OpenAI."),
    ("openai_endpoint_request_timeout", "Request timeout (seconds)", "int",
     "How long to wait for that endpoint before giving up."),
    ("max_prompt_length", "Maximum prompt length", "int",
     "Longest custom prompt a server may set."),
    ("max_random_prompt_length", "Maximum random prompt length", "int",
     "Longest unprompted-message prompt a server may set."),
))

ALL_FIELDS = tuple(f for _title, _icon, fields in SECTIONS for f in fields)
OWNER_FIELDS = OWNER_SECTION[2]


def _lines_to_list(raw: str) -> list:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _ids_to_list(raw: t.Iterable) -> list[int]:
    out = []
    for value in raw or []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


@dashboard_page(
    name="settings",
    description="Configure AIUser for this server.",
    methods=("GET", "POST"),
)
async def server_settings(
    self: MixinMeta, user: discord.User, guild: discord.Guild, **kwargs
):
    import wtforms

    conf = self.config.guild(guild)
    current = await conf.all()
    is_owner = await self.bot.is_owner(user)
    global_current = await self.config.all() if is_owner else {}

    channel_choices = [(str(c.id), f"#{c.name}") for c in guild.text_channels]
    role_choices = [(str(r.id), r.name) for r in guild.roles if not r.is_default()]
    member_choices = [(str(m.id), m.display_name) for m in guild.members if not m.bot][:500]

    def build(key, label, kind, helptext, source=None):
        render = {"title": helptext} if helptext else {}
        value = (source if source is not None else current).get(key)
        if kind == "bool":
            return wtforms.BooleanField(label, default=bool(value), render_kw=render)
        if kind == "int":
            return wtforms.IntegerField(label, default=int(value or 0), render_kw=render)
        if kind == "float":
            return wtforms.FloatField(label, default=float(value or 0), render_kw=render)
        if kind == "lines":
            return wtforms.TextAreaField(
                label, default="\n".join(str(v) for v in (value or [])),
                render_kw={**render, "rows": 4},
            )
        if kind == "text":
            import json
            shown = "" if value in (None, "") else (
                json.dumps(value, indent=2) if isinstance(value, (dict, list)) else str(value)
            )
            return wtforms.TextAreaField(label, default=shown, render_kw={**render, "rows": 4})
        if kind in ("channels", "roles", "members"):
            choices = {"channels": channel_choices, "roles": role_choices,
                       "members": member_choices}[kind]
            return wtforms.SelectMultipleField(
                label, choices=choices,
                default=[str(v) for v in (value or [])], render_kw=render,
            )
        return wtforms.StringField(
            label, default="" if value is None else str(value), render_kw=render,
        )

    attrs = {f[0]: build(*f) for f in ALL_FIELDS}
    if is_owner:
        attrs.update({f[0]: build(*f, source=global_current) for f in OWNER_FIELDS})
    attrs["submit"] = wtforms.SubmitField("Save Settings")
    attrs["__init__"] = lambda s: super(type(s), s).__init__(prefix="aiuser_settings_")
    Form = type("AIUserSettingsForm", (kwargs["Form"],), attrs)
    form = Form()

    notifications = []
    if form.validate_on_submit():
        import json

        for key, _label, kind, _help in ALL_FIELDS:
            field = getattr(form, key)
            raw = field.data
            if kind == "bool":
                value = bool(raw)
            elif kind in ("int", "float"):
                value = raw if raw is not None else 0
            elif kind == "lines":
                value = _lines_to_list(raw)
            elif kind in ("channels", "roles", "members"):
                value = _ids_to_list(raw)
            elif kind == "text":
                text = (raw or "").strip()
                if not text:
                    value = None
                else:
                    try:
                        value = json.loads(text)
                    except ValueError:
                        notifications.append(
                            {"message": f"{_label}: that is not valid JSON, so it was "
                                        "left unchanged.", "category": "warning"}
                        )
                        continue
            else:
                text = (raw or "").strip()
                value = text or None
            await conf.get_attr(key).set(value)

        # Bot-wide values, guarded again here rather than trusting that the
        # fields only exist for owners.
        if is_owner:
            for key, _label, kind, _help in OWNER_FIELDS:
                raw = getattr(form, key).data
                if kind == "int":
                    await self.config.get_attr(key).set(int(raw or 0))
                else:
                    await self.config.get_attr(key).set((raw or "").strip() or None)

        notifications.insert(0, {"message": "Settings saved", "category": "success"})
        return {
            "status": 0,
            "notifications": notifications,
            "redirect_url": kwargs["request_url"],
        }

    sections = [
        {"title": title, "icon": icon, "fields": [f[0] for f in fields]}
        for title, icon, fields in (SECTIONS + ((OWNER_SECTION,) if is_owner else ()))
    ]

    return {
        "status": 0,
        "web_content": {"source": _render(form, sections)},
    }


def _render(form: t.Any, sections: list) -> str:
    """Render the page here, on the bot, where the form object still exists.

    A `Form` handed back in `web_content` is stringified before it crosses RPC
    (see `DashboardRPC_ThirdParties.data_receive`), so the dashboard would
    receive text and `settings_form.hidden_tag()` would fail on a str. Laying
    the fields out per section needs the real object, so the template is
    rendered here and the dashboard is given finished HTML.

    Nothing in the template needs Flask globals such as `url_for`; if that ever
    changes, the part that needs them has to be wrapped in `{% raw %}` so the
    dashboard's own render resolves it instead.
    """
    import jinja2

    template = (TEMPLATES_PATH / "settings_page.html").read_text(encoding="utf-8")
    env = jinja2.Environment(autoescape=True)
    return env.from_string(template).render(settings_form=form, sections=sections)
