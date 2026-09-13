"""The memory management page.

Lets a server admin see, add, and delete the long-term memory facts the
model can save about users/channels via the save_memory tool call (see
functions/memory/tool_call.py), and add facts by hand without needing the
model to have said them first.
"""

import pathlib

import discord

from ..dashboard.decorator import dashboard_page
from ..types.abc import MixinMeta

TEMPLATES_PATH = pathlib.Path(__file__).parent / "templates"


def _resolve_user(guild: discord.Guild, user_id: str) -> str:
    if not user_id:
        return "Everyone"
    member = guild.get_member(int(user_id))
    return member.display_name if member else f"Unknown user ({user_id})"


def _resolve_channel(guild: discord.Guild, channel_id: str) -> str:
    if not channel_id:
        return "Every channel"
    channel = guild.get_channel(int(channel_id))
    return f"#{channel.name}" if channel else f"Unknown channel ({channel_id})"


@dashboard_page(
    name="memories",
    description="View, add, and remove long-term memories for this server.",
    methods=("GET", "POST"),
)
async def memories_page(self: MixinMeta, guild: discord.Guild, **kwargs):
    import wtforms

    db = self.services.memories
    rows = await db.list_detailed(guild.id)

    member_choices = [("", "Everyone")] + [
        (str(m.id), m.display_name) for m in guild.members if not m.bot
    ][:500]
    channel_choices = [("", "Every channel")] + [
        (str(c.id), f"#{c.name}") for c in guild.text_channels
    ]

    attrs = {}
    for row_id, *_rest in rows:
        attrs[f"delete_{row_id}"] = wtforms.SubmitField(
            "Delete", render_kw={"class": "btn btn-sm btn-outline-danger"}
        )
    attrs["memory_name"] = wtforms.StringField(
        "Name",
        render_kw={"placeholder": "e.g. favorite_boss, ongoing_event"},
    )
    attrs["memory_text"] = wtforms.TextAreaField(
        "Fact", render_kw={"rows": 3, "placeholder": "The detailed fact to remember."}
    )
    attrs["scope_user"] = wtforms.SelectField(
        "Only for user", choices=member_choices, default=""
    )
    attrs["scope_channel"] = wtforms.SelectField(
        "Only in channel", choices=channel_choices, default=""
    )
    attrs["add_submit"] = wtforms.SubmitField("Add memory")
    attrs["__init__"] = lambda s: super(type(s), s).__init__(prefix="aiuser_memories_")
    Form = type("AIUserMemoriesForm", (kwargs["Form"],), attrs)
    form = Form()

    notifications = []
    if form.validate_on_submit():
        deleted_name = None
        for row_id, row_name, *_rest in rows:
            field = getattr(form, f"delete_{row_id}", None)
            if field is not None and field.data:
                if await db.delete(row_id, guild.id):
                    deleted_name = row_name
                break

        if deleted_name:
            notifications.append(
                {"message": f"Deleted memory \"{deleted_name}\".", "category": "success"}
            )
        elif form.add_submit.data:
            name = (form.memory_name.data or "").strip()
            text = (form.memory_text.data or "").strip()
            if not name or not text:
                notifications.append(
                    {
                        "message": "Both a name and a fact are required to add a memory.",
                        "category": "warning",
                    }
                )
            else:
                try:
                    await db.upsert(
                        guild.id,
                        name,
                        text,
                        user=form.scope_user.data or None,
                        channel=form.scope_channel.data or None,
                    )
                    notifications.append(
                        {"message": f"Saved memory \"{name}\".", "category": "success"}
                    )
                except ValueError:
                    notifications.append(
                        {
                            "message": "That user or channel scope wasn't valid.",
                            "category": "danger",
                        }
                    )

        if notifications:
            return {
                "status": 0,
                "notifications": notifications,
                "redirect_url": kwargs["request_url"],
            }

    display_rows = [
        {
            "id": row_id,
            "name": row_name,
            "text": row_text,
            "user": _resolve_user(guild, row_user),
            "channel": _resolve_channel(guild, row_channel),
            "delete_field": f"delete_{row_id}",
        }
        for row_id, row_name, row_text, row_user, row_channel in rows
    ]

    template_path = TEMPLATES_PATH / "memories_page.html"
    source = _render(template_path.read_text(encoding="utf-8"), form=form, rows=display_rows)

    return {
        "status": 0,
        "notifications": notifications,
        "web_content": {"source": source},
    }


def _render(template: str, **context) -> str:
    """Render here, on the bot, for the same reason settings_page.py does:
    a `Form` in `web_content` is stringified before crossing RPC, so the
    dashboard's own render can't work with the live object.
    """
    import jinja2

    env = jinja2.Environment(autoescape=True)
    html = env.from_string(template).render(**context)
    return "{% raw %}" + html.replace("{% endraw %}", "{% endraw %}{% raw %}") + "{% endraw %}"
