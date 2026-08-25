"""Helpers shared by the hand-written dashboard integrations in redbot/cogs/*.

Lives in core rather than in the Dashboard cog so that importing it never makes
a cog depend on the Dashboard being loaded.
"""
from __future__ import annotations

import typing as t

import discord

__all__ = (
    "dashboard_page",
    "form_reader",
    "is_staff",
    "guild_member",
    "channel_options",
    "role_options",
    "notify",
    "BASE_CSS",
)


def dashboard_page(*args: t.Any, **kwargs: t.Any) -> t.Callable:
    """Mark a coroutine as a dashboard page.

    A local copy of the Dashboard cog's decorator; the third-party handler
    re-applies its own on registration.
    """

    def decorator(func: t.Callable) -> t.Callable:
        func.__dashboard_decorator_params__ = (args, kwargs)
        return func

    return decorator


def form_reader(kwargs: dict) -> t.Callable[[str, t.Any], t.Any]:
    """Return a getter for POST fields.

    The webserver builds the form with ``request.form.to_dict(flat=False)``, so
    every value arrives as a list even when it is single-valued.
    """
    form = (kwargs.get("data") or {}).get("form") or {}

    def field(key: str, default: t.Any = None) -> t.Any:
        value = form.get(key, default)
        if isinstance(value, (list, tuple)):
            return value[-1] if value else default
        return value

    field.raw = form  # type: ignore[attr-defined]
    field.many = lambda key: [x for x in (form.get(key) or []) if x not in ("", None)]  # type: ignore[attr-defined]
    field.checked = lambda key: key in form  # type: ignore[attr-defined]
    return field


async def is_staff(bot, user: discord.User, member: discord.Member, guild: discord.Guild) -> bool:
    return (
        await bot.is_owner(user)
        or member.id == guild.owner_id
        or member.guild_permissions.administrator
        or await bot.is_admin(member)
    )


def guild_member(user: discord.User, guild: discord.Guild):
    """Return (member, error_dict). Error is None when the user is a member."""
    member = guild.get_member(user.id)
    if member is None:
        return None, {
            "status": 1,
            "error_title": "Not a member",
            "error_message": "You are not a member of this guild.",
        }
    return member, None


def channel_options(guild: discord.Guild, *, selected: int | None = None, kinds=("text",)):
    """Serialisable channel list for a <select>."""
    wanted = []
    if "text" in kinds:
        wanted.extend(guild.text_channels)
    if "voice" in kinds:
        wanted.extend(guild.voice_channels)
    if "category" in kinds:
        wanted.extend(guild.categories)
    return [
        {"id": str(c.id), "name": f"#{c.name}", "selected": selected is not None and c.id == selected}
        for c in sorted(wanted, key=lambda c: (getattr(c, "position", 0), c.name))
    ]


def role_options(guild: discord.Guild, *, selected: int | None = None, skip_default: bool = True):
    roles = [r for r in guild.roles if not (skip_default and r.is_default())]
    return [
        {"id": str(r.id), "name": r.name, "selected": selected is not None and r.id == selected}
        for r in sorted(roles, key=lambda r: -r.position)
    ]


def notify(message: str, category: str = "success", *, redirect: str | None = None) -> dict:
    payload = {"status": 0, "notifications": [{"message": message, "category": category}]}
    if redirect:
        payload["redirect_url"] = redirect
    return payload


# Shared look-and-feel, kept consistent with the plcontroller page.
BASE_CSS = """
<style>
  .dz { display:flex; flex-direction:column; gap:16px; }
  .dz-head { padding:16px 20px; border-radius:14px;
             background:rgba(24,48,105,.22); border:1px solid rgba(130,175,255,.16); }
  .dz-head h4 { margin:0 0 3px; font-size:1.05rem; }
  .dz-head p  { margin:0; opacity:.65; font-size:.85rem; }
  .dz-panel { padding:16px; border-radius:14px;
              background:rgba(90,130,220,.06); border:1px solid rgba(120,160,255,.12); }
  .dz-panel h5 { margin:0 0 3px; font-size:.95rem; }
  .dz-hint { opacity:.6; font-size:.78rem; margin:0 0 11px; }
  .dz-grid { display:grid; gap:14px; grid-template-columns:1fr; }
  @media (min-width:1000px){ .dz-grid.two { grid-template-columns:1fr 1fr; } }
  .dz-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .dz-label { display:flex; align-items:center; gap:8px; font-weight:600;
              font-size:.86rem; margin-bottom:6px; }
  .dz-input, .dz-select, .dz-area {
    width:100%; padding:9px 12px; border-radius:10px; color:inherit; font-size:.88rem;
    background:rgba(0,0,0,.3); border:1px solid rgba(255,255,255,.12);
  }
  .dz-area { min-height:90px; resize:vertical; font-family:inherit; }
  .dz-input:focus, .dz-select:focus, .dz-area:focus {
    outline:none; border-color:rgba(130,175,255,.45); }
  .dz-btn { display:inline-flex; align-items:center; justify-content:center; gap:7px;
            height:42px; padding:0 16px; border-radius:11px; cursor:pointer;
            font-size:.87rem; font-weight:600; color:inherit; text-decoration:none;
            background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.12); }
  .dz-btn:hover { background:rgba(255,255,255,.12); }
  .dz-btn.primary { background:linear-gradient(135deg,#2f6fed,#5aa9ff);
                    border-color:transparent; color:#fff; }
  .dz-btn.danger  { border-color:rgba(255,90,90,.45); color:#ff8b8b; }
  .dz-btn.round   { width:42px; padding:0; border-radius:50%; }
  .dz-toggle { display:flex; align-items:center; gap:9px; cursor:pointer;
               padding:9px 0; font-size:.86rem; }
  .dz-t { width:100%; border-collapse:collapse; }
  .dz-t th, .dz-t td { text-align:left; padding:9px 10px; font-size:.85rem;
                       border-bottom:1px solid rgba(255,255,255,.06); }
  .dz-t th { opacity:.55; font-size:.69rem; text-transform:uppercase; letter-spacing:.05em; }
  .dz-t tr:last-child td { border-bottom:none; }
  .dz-empty { opacity:.6; padding:20px; text-align:center; font-size:.86rem; }
  .dz-tag { font-size:.68rem; padding:2px 9px; border-radius:999px;
            background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.12); }
  .dz-save { position:sticky; bottom:0; padding:12px 0; }
</style>
"""
