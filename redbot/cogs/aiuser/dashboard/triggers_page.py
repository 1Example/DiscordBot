"""Manage phrases that request an AI reply grounded in saved facts."""

import pathlib

import discord

from ..context.triggered_memory import MAX_ENTRIES, MAX_FACT_LENGTH, make_entry
from ..dashboard.decorator import dashboard_page
from ..dashboard.memories_page import _render, _resolve_channel, _resolve_user
from ..types.abc import MixinMeta


@dashboard_page(
    name="triggers",
    description="Trigger words and information for automatic AI replies.",
    methods=("GET", "POST"),
)
async def triggers_page(self: MixinMeta, user: discord.User, guild: discord.Guild, **kwargs):
    member = guild.get_member(user.id)
    if not (await self.bot.is_owner(user) or (
        member and (member.guild_permissions.manage_guild or await self.bot.is_admin(member))
    )):
        return {
            "status": 1, "error_code": 403, "error_title": "Forbidden",
            "error_message": "Only server administrators can manage information triggers.",
        }

    import wtforms

    conf = self.config.guild(guild)
    entries = await conf.triggered_memories()
    entries = entries if isinstance(entries, list) else []
    memories = {row[0]: row for row in await self.services.memories.list_detailed(guild.id)}
    choices = [("", "Enter information below")]
    choices.extend((str(row[0]), f"{row[1]} — {_resolve_user(guild, row[3])}, "
                    f"{_resolve_channel(guild, row[4])}") for row in memories.values())
    # Let admins edit/delete entries even if the linked memory was removed.
    missing_ids = {e.get("memory_id") for e in entries if e.get("memory_id") not in memories}
    choices.extend((str(mid), f"Deleted memory ({mid})") for mid in missing_ids if mid)
    attrs = {
        "entry_id": wtforms.HiddenField(),
        "name": wtforms.StringField("Name", render_kw={"maxlength": 80}),
        "phrases": wtforms.TextAreaField("Trigger words or phrases", render_kw={"rows": 4}),
        "memory_id": wtforms.SelectField("Information source", choices=choices),
        "information": wtforms.TextAreaField("Information", render_kw={"rows": 5, "maxlength": MAX_FACT_LENGTH}),
        "enabled": wtforms.BooleanField("Enabled", default=True),
        "save": wtforms.SubmitField("Save trigger"),
        "cancel": wtforms.SubmitField("New entry"),
    }
    for entry in entries:
        attrs[f"edit_{entry['id']}"] = wtforms.SubmitField("Edit")
        attrs[f"delete_{entry['id']}"] = wtforms.SubmitField("Delete")
    attrs["__init__"] = lambda s: super(type(s), s).__init__(prefix="aiuser_triggers_")
    form = type("AIUserTriggersForm", (kwargs["Form"],), attrs)()
    notifications = []

    if form.validate_on_submit():
        edit = next((e for e in entries if getattr(form, f"edit_{e['id']}").data), None)
        delete = next((e for e in entries if getattr(form, f"delete_{e['id']}").data), None)
        if delete:
            async with conf.triggered_memories() as current:
                current[:] = [e for e in current if e["id"] != delete["id"]]
            return {
                "status": 0, "redirect_url": kwargs["request_url"],
                "notifications": [{"message": "Trigger deleted.", "category": "success"}],
            }
        if edit:
            form.entry_id.data = edit["id"]
            form.name.data = edit["name"]
            form.phrases.data = "\n".join(edit["phrases"])
            form.memory_id.data = str(edit["memory_id"]) if edit.get("memory_id") else ""
            form.information.data = edit.get("text", "")
            form.enabled.data = edit.get("enabled", True)
        elif form.cancel.data:
            return {"status": 0, "redirect_url": kwargs["request_url"]}
        elif form.save.data:
            try:
                memory_id = int(form.memory_id.data) if form.memory_id.data else None
                if memory_id is not None:
                    row = memories.get(memory_id)
                    if not row:
                        raise ValueError("That memory no longer exists; choose another source.")
                    if not row[2].strip() or len(row[2]) > MAX_FACT_LENGTH:
                        raise ValueError(f"Choose a memory with 1–{MAX_FACT_LENGTH} characters of information.")
                entry = make_entry(
                    form.name.data or "", form.phrases.data or "",
                    form.information.data or "", memory_id=memory_id,
                    enabled=form.enabled.data, entry_id=form.entry_id.data or None,
                )
                async with conf.triggered_memories() as current:
                    index = next((i for i, e in enumerate(current) if e["id"] == entry["id"]), None)
                    if form.entry_id.data and index is None:
                        raise ValueError("This trigger was deleted. Reload the page to create a new entry.")
                    if index is not None:
                        current[index] = entry
                    elif len(current) >= MAX_ENTRIES:
                        raise ValueError(f"A server can have up to {MAX_ENTRIES} trigger entries.")
                    else:
                        current.append(entry)
                return {
                    "status": 0, "redirect_url": kwargs["request_url"],
                    "notifications": [{"message": "Trigger saved.", "category": "success"}],
                }
            except ValueError as exc:
                notifications.append({"message": str(exc), "category": "warning"})

    display = []
    for entry in entries:
        row = memories.get(entry.get("memory_id"))
        source = "Custom information"
        if entry.get("memory_id"):
            source = f"Memory: {row[1]}" if row else "Memory deleted — trigger inactive"
        display.append({**entry, "source": source,
                        "information": row[2] if row else entry.get("text", "")})
    template = (pathlib.Path(__file__).parent / "templates" / "triggers_page.html").read_text(encoding="utf-8")
    return {
        "status": 0, "notifications": notifications,
        "web_content": {"source": _render(template, form=form, entries=display)},
    }
