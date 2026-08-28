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
    "query_reader",
    "fake_context",
    "is_staff",
    "guild_member",
    "channel_options",
    "role_options",
    "member_options",
    "emoji_options",
    "message_preview",
    "MACROS",
    "notify",
    "BASE_CSS",
    "NOTIFICATIONS",
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

    The webserver builds the form with ``request.form.to_dict(flat=False)`` and
    the RPC layer hands it over as a ``werkzeug`` ``ImmutableMultiDict``. On that
    type ``get`` returns only the *first* value, so reading a multi-select needs
    ``getlist``; a plain dict of lists is also accepted so the helper stays
    usable outside a request.
    """
    form = (kwargs.get("data") or {}).get("form") or {}

    def values(key: str) -> list:
        """Every value submitted under `key`, whatever container `form` is."""
        getlist = getattr(form, "getlist", None)
        if getlist is not None:
            return list(getlist(key))
        raw = form.get(key)
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            return list(raw)
        return [raw]

    def field(key: str, default: t.Any = None) -> t.Any:
        found = values(key)
        if not found:
            return default
        return found[-1]

    field.raw = form  # type: ignore[attr-defined]
    field.values = values  # type: ignore[attr-defined]
    field.many = lambda key: [x for x in values(key) if x not in ("", None)]  # type: ignore[attr-defined]
    field.checked = lambda key: key in form  # type: ignore[attr-defined]

    def integer(key: str, default=None):
        try:
            return int(str(field(key)).strip())
        except (TypeError, ValueError):
            return default

    field.integer = integer  # type: ignore[attr-defined]
    return field


def emoji_problem(raw: str) -> str:
    """Why `raw` cannot be used as a button emoji, phrased for the page.

    Discord accepts exactly two things on a button: one unicode emoji, or a
    custom-emoji token. Everything else has to be explained, because "not
    usable" leaves the person staring at a box with no idea what to change.
    """
    raw = (raw or "").strip()
    if not raw:
        return "it was empty"
    if raw.startswith(("http://", "https://")):
        return (
            "that is a link - use the upload button for a picture, or paste a "
            "<:name:id> token"
        )
    if raw.startswith("<") and raw.endswith(">"):
        return (
            "that looks like a custom emoji token but Discord did not accept it; "
            "the id must be the numeric one, as in <:name:123456789012345678>"
        )
    if len(raw) > 2 and raw.startswith(":") and raw.endswith(":"):
        return "shortcodes like :name: do not work - paste the <:name:id> token instead"
    if raw.isascii():
        return "plain text is not an emoji - paste a real one, or a <:name:id> token"
    if len(raw) > 8:
        return f"it is {len(raw)} characters long; a single emoji is expected"
    return "Discord did not recognise it as an emoji"


def emoji_rejection(key: str, raw: str, *, limit: int = 40) -> dict:
    """A ready-made warning notification for a rejected emoji."""
    shown = (raw or "").strip()
    if len(shown) > limit:
        shown = shown[:limit] + "..."
    return {
        "message": f"{key}: {emoji_problem(raw)}. Got: {shown!r}",
        "category": "warning",
    }


async def fake_context(bot, member: discord.Member, content: str, channel=None):
    """Build a real `Context` for a command that was triggered from the dashboard.

    Some cog internals invoke a configured command string (a warning action, a
    role-tools command) by copying the invoking message. There is no message
    behind a dashboard request, so one is synthesised in a channel the bot can
    talk in and handed to `bot.get_context` as usual.

    Returns `None` when no usable channel exists or discord.py rejects the
    payload; callers should treat that as "the automated step was skipped".
    """
    from datetime import datetime, timezone

    guild = member.guild
    if channel is None:
        channel = next(
            (
                c
                for c in guild.text_channels
                if c.permissions_for(guild.me).send_messages
            ),
            None,
        )
    if channel is None:
        return None

    payload = {
        # The ID only has to be unique and parseable; nothing fetches it.
        "id": str(discord.utils.time_snowflake(datetime.now(tz=timezone.utc))),
        "type": 0,
        "content": content,
        "channel_id": str(channel.id),
        "guild_id": str(guild.id),
        "author": {
            "id": str(member.id),
            "username": member.name,
            "discriminator": getattr(member, "discriminator", "0"),
            "avatar": None,
            "bot": False,
        },
        "attachments": [],
        "embeds": [],
        "mentions": [],
        "mention_roles": [],
        "pinned": False,
        "mention_everyone": False,
        "tts": False,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "edited_timestamp": None,
        "flags": 0,
    }
    try:
        message = channel._state.create_message(channel=channel, data=payload)
        message.author = member
    except Exception:  # noqa: BLE001 - discord.py internals vary between versions
        return None
    return await bot.get_context(message)


def query_reader(kwargs: dict) -> t.Callable[[str, t.Any], t.Any]:
    """Return a getter for URL query arguments.

    Anything in the query string that a page did not declare as a required or
    optional kwarg arrives in ``extra_kwargs``, which is how a GET form on a
    page passes filters, search terms and page numbers back to itself.
    """
    args = kwargs.get("extra_kwargs") or {}

    def field(key: str, default: t.Any = None) -> t.Any:
        value = args.get(key, default)
        if isinstance(value, (list, tuple)):
            return value[-1] if value else default
        return value

    field.raw = args  # type: ignore[attr-defined]

    def integer(key: str, default=None):
        try:
            return int(str(field(key)).strip())
        except (TypeError, ValueError):
            return default

    field.integer = integer  # type: ignore[attr-defined]
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


def _kind_of(channel) -> str:
    if isinstance(channel, discord.CategoryChannel):
        return "category"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    if isinstance(channel, discord.StageChannel):
        return "stage"
    return "text"


def channel_options(
    guild: discord.Guild,
    *,
    selected: int | None = None,
    selected_many=None,
    kinds=("text",),
    require_send: bool = False,
):
    """Channel list for a picker, grouped by category.

    `require_send` marks channels the bot cannot post in, so a page can warn
    rather than letting the action fail silently later.
    """
    chosen = {str(i) for i in (selected_many or [])}
    wanted = []
    if "text" in kinds:
        wanted.extend(guild.text_channels)
    if "voice" in kinds:
        wanted.extend(guild.voice_channels)
    if "stage" in kinds:
        wanted.extend(getattr(guild, "stage_channels", []))
    if "category" in kinds:
        wanted.extend(guild.categories)
    if "forum" in kinds:
        wanted.extend(getattr(guild, "forums", []))

    me = guild.me
    prefixes = {"text": "#", "voice": "\N{SPEAKER WITH THREE SOUND WAVES}",
                "stage": "\N{STUDIO MICROPHONE}", "category": "\N{BLACK RIGHT-POINTING SMALL TRIANGLE}",
                "forum": "\N{SPEECH BALLOON}"}
    out = []
    for channel in sorted(wanted, key=lambda c: (getattr(c, "position", 0), c.name)):
        can_send = True
        if require_send and me is not None and hasattr(channel, "permissions_for"):
            can_send = bool(getattr(channel.permissions_for(me), "send_messages", False))
        out.append(
            {
                "id": str(channel.id),
                "name": f"{prefixes.get(_kind_of(channel), '#')} {channel.name}",
                "group": getattr(getattr(channel, "category", None), "name", "") or "No category",
                "selected": (selected is not None and channel.id == selected)
                or str(channel.id) in chosen,
                "warn": not can_send,
            }
        )
    return out


def role_options(
    guild: discord.Guild,
    *,
    selected: int | None = None,
    selected_many=None,
    skip_default: bool = True,
    skip_managed: bool = False,
):
    chosen = {str(i) for i in (selected_many or [])}
    me_top = guild.me.top_role.position if guild.me else 0
    out = []
    for role in sorted(guild.roles, key=lambda r: -r.position):
        if skip_default and role.is_default():
            continue
        if skip_managed and role.managed:
            continue
        out.append(
            {
                "id": str(role.id),
                "name": role.name,
                # Roles at or above the bot's top role can never be assigned.
                "group": "Above me" if role.position >= me_top else "Assignable",
                "colour": f"#{role.colour.value:06x}" if role.colour.value else "#99aab5",
                "members": len(role.members),
                "selected": (selected is not None and role.id == selected)
                or str(role.id) in chosen,
                "warn": role.position >= me_top,
            }
        )
    return out


def member_options(guild: discord.Guild, *, selected: int | None = None,
                   limit: int = 500, humans_only: bool = False):
    members = [m for m in guild.members if not (humans_only and m.bot)]
    members.sort(key=lambda m: m.display_name.lower())
    return [
        {
            "id": str(m.id),
            "name": m.display_name,
            "group": "Bots" if m.bot else "Members",
            "selected": selected is not None and m.id == selected,
            "warn": False,
        }
        for m in members[:limit]
    ]


def emoji_options(guild: discord.Guild, *, selected: str | None = None):
    """Custom emoji available here, as `<:name:id>` tokens."""
    out = []
    for emoji in sorted(guild.emojis, key=lambda e: e.name.lower()):
        token = f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>"
        out.append(
            {
                "id": token,
                "name": f":{emoji.name}:",
                "group": "Animated" if emoji.animated else "Static",
                "url": str(emoji.url),
                "selected": selected == token,
                "warn": False,
            }
        )
    return out


def message_preview(message, *, max_chars: int = 400) -> dict:
    """Flatten a discord.Message into something the `msg` macro can render."""
    from datetime import datetime, timezone

    content = message.content or ""
    if len(content) > max_chars:
        content = content[:max_chars] + "\N{HORIZONTAL ELLIPSIS}"

    embeds = []
    for embed in message.embeds[:2]:
        embeds.append(
            {
                "title": embed.title or "",
                "description": (embed.description or "")[:300],
                "colour": f"#{embed.colour.value:06x}" if embed.colour else "#4f545c",
                "footer": (embed.footer.text if embed.footer else "") or "",
                "fields": [
                    {"name": f.name, "value": (f.value or "")[:120]}
                    for f in embed.fields[:6]
                ],
            }
        )

    created = message.created_at or datetime.now(timezone.utc)
    author = message.author
    return {
        "id": str(message.id),
        "author": getattr(author, "display_name", str(author)),
        "avatar": str(getattr(author, "display_avatar", "") or ""),
        "bot": bool(getattr(author, "bot", False)),
        "content": content,
        "timestamp": created.strftime("%d %b %Y, %H:%M"),
        "attachments": [a.filename for a in message.attachments[:4]],
        "embeds": embeds,
        "pinned": bool(message.pinned),
        # Discord refuses to bulk-delete anything older than 14 days.
        "old": (datetime.now(timezone.utc) - created).days >= 14,
    }


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


# Additional styling for the v2 components: searchable pickers, stat strips and
# the Discord-style message preview.
BASE_CSS += """
<style>
  .dz-grid.three { grid-template-columns:1fr; }
  @media (min-width:1200px){ .dz-grid.three { grid-template-columns:repeat(3,1fr); } }
  .dz-tag.warn { color:#f0aa3c; border-color:rgba(240,170,60,.4); }
  .dz-tag.bad  { color:#ff8b8b; border-color:rgba(255,90,90,.4); }
  .dz-tag.good { color:#3ba55d; border-color:rgba(59,165,93,.4); }

  /* A select is width:100% and a flex item shrinks: put one in a .dz-row
     next to a button and it collapses to the width of its own arrow, which
     leaves the open dropdown rendering one character per line. Give the
     controls a floor and stop the buttons being squeezed instead. */
  .dz-row > .dz-select, .dz-row > .dz-input, .dz-row > .dz-area,
  .dz-row > .dz-pick { flex:1 1 220px; min-width:170px; width:auto; }
  .dz-row > .dz-btn, .dz-row > button { flex:0 0 auto; }
  /* Multi-selects size themselves from `size`; the single-line padding used
     for a dropdown squashes them. */
  select.dz-select[multiple] { height:auto; padding:6px; }
  select.dz-select[multiple] option { padding:3px 6px; border-radius:6px; }

  .dz-pick { position:relative; }
  .dz-pick input.dz-search { margin-bottom:6px; }
  .dz-pick .dz-count { font-size:.7rem; opacity:.45; margin-top:4px; }

  .dz-stats { display:flex; gap:10px; flex-wrap:wrap; }
  .dz-stat { flex:1 1 120px; padding:11px 14px; border-radius:12px;
             background:rgba(0,0,0,.22); border:1px solid rgba(255,255,255,.08); }
  .dz-stat b { display:block; font-size:1.25rem; line-height:1.2; }
  .dz-stat span { font-size:.72rem; opacity:.5; }

  .dz-msgs { display:flex; flex-direction:column; gap:2px;
             max-height:460px; overflow-y:auto; padding:4px 2px; }
  .dz-msg { display:flex; gap:11px; padding:7px 9px; border-radius:8px; }
  .dz-msg:hover { background:rgba(255,255,255,.03); }
  .dz-msg.old { opacity:.5; }
  .dz-av { width:34px; height:34px; border-radius:50%; flex:0 0 auto;
           background:rgba(255,255,255,.09); object-fit:cover; }
  .dz-body { min-width:0; flex:1; }
  .dz-meta { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; }
  .dz-name { font-weight:600; font-size:.88rem; }
  .dz-time { font-size:.68rem; opacity:.4; }
  .dz-text { font-size:.86rem; line-height:1.4; white-space:pre-wrap;
             word-break:break-word; opacity:.9; }
  .dz-att { font-size:.72rem; opacity:.55; margin-top:3px; }
  .dz-embed { margin-top:5px; padding:9px 12px; border-radius:5px;
              border-left:4px solid #4f545c; background:rgba(0,0,0,.26); max-width:440px; }
  .dz-embed .et { font-weight:600; font-size:.85rem; margin-bottom:3px; }
  .dz-embed .ed { font-size:.8rem; opacity:.82; white-space:pre-wrap; }
  .dz-embed .ef { font-size:.7rem; opacity:.5; margin-top:6px; }
  .dz-embed .efield { margin-top:6px; font-size:.78rem; }
  .dz-embed .efield b { display:block; opacity:.9; }
  .dz-botpill { font-size:.6rem; padding:1px 5px; border-radius:3px;
                background:#5865f2; color:#fff; font-weight:700; }
  .dz-emoji { width:20px; height:20px; vertical-align:-4px; }
</style>
"""


# Toasts, shared by every dashboard page.
#
# The webserver renders its own notifications wherever its layout happens to
# put them, in whatever the underlying CSS framework calls an alert. This
# adopts those into one fixed, top-right stack with a consistent look, and
# gives pages ``dzToast(message, category)`` so anything they raise themselves
# lands in the same place. Appended to BASE_CSS, so importing that is all a cog
# has to do.
NOTIFICATIONS = r"""
<style>
  .dz-toasts{
    position:fixed; top:16px; right:16px; z-index:99999;
    display:flex; flex-direction:column; gap:9px;
    width:min(380px, calc(100vw - 32px));
    pointer-events:none;
  }
  .dz-toast{
    display:flex; align-items:flex-start; gap:11px;
    padding:12px 14px; border-radius:12px;
    background:#151a30; border:1px solid rgba(255,255,255,.16);
    border-left:3px solid #6c8cff;
    color:#eef2ff; font-size:.85rem; line-height:1.4;
    box-shadow:0 14px 38px rgba(0,0,0,.55);
    animation:dzToastIn .22s cubic-bezier(.2,.9,.3,1);
    pointer-events:auto; overflow:hidden;
  }
  .dz-toast > i{ margin-top:2px; flex:0 0 auto; color:#6c8cff; }
  .dz-toast-body{ min-width:0; flex:1 1 auto; word-break:break-word; }
  .dz-toast-x{
    flex:0 0 auto; width:22px; height:22px; border:none; border-radius:6px;
    background:transparent; color:inherit; opacity:.45; cursor:pointer;
    font-size:.9rem; line-height:1; padding:0;
  }
  .dz-toast-x:hover{ opacity:1; background:rgba(255,255,255,.10); }
  .dz-toast.success{ border-left-color:#38d39f; } .dz-toast.success > i{ color:#38d39f; }
  .dz-toast.warning{ border-left-color:#ffb454; } .dz-toast.warning > i{ color:#ffb454; }
  .dz-toast.danger { border-left-color:#ff6b6b; } .dz-toast.danger  > i{ color:#ff6b6b; }
  .dz-toast.info   { border-left-color:#6c8cff; }
  .dz-toast.out{ animation:dzToastOut .2s ease-in forwards; }
  @keyframes dzToastIn { from{ opacity:0; transform:translateX(24px); } to{ opacity:1; transform:none; } }
  @keyframes dzToastOut{ to{ opacity:0; transform:translateX(24px); height:0;
                             padding-top:0; padding-bottom:0; margin-bottom:-9px; } }
  /* An adopted alert keeps its own framework classes; neutralise the parts of
     them that would fight the toast look. */
  .dz-toast.alert{ margin:0; }
  .dz-toast .close, .dz-toast .btn-close{ display:none !important; }
  @media (prefers-reduced-motion:reduce){
    .dz-toast, .dz-toast.out{ animation:none; }
  }
</style>
{% raw %}
<script>
(function () {
  "use strict";
  if (window.dzToast) return;          // one implementation per page

  var STACK = null;
  function stack() {
    if (STACK && document.body.contains(STACK)) return STACK;
    STACK = document.createElement("div");
    STACK.className = "dz-toasts";
    STACK.setAttribute("role", "status");
    STACK.setAttribute("aria-live", "polite");
    document.body.appendChild(STACK);
    return STACK;
  }

  var ICONS = {
    success: "fa-check-circle", warning: "fa-exclamation-triangle",
    danger: "fa-times-circle", info: "fa-info-circle"
  };
  // How long each kind stays. An error is the one you most want to still be
  // there when you look back at the screen.
  var LIFE = { danger: 9000, warning: 7000, success: 4000, info: 5000 };

  function dismiss(el) {
    if (!el || el.dataset.dzGone) return;
    el.dataset.dzGone = "1";
    el.classList.add("out");
    setTimeout(function () { el.remove(); }, 220);
  }

  function mount(el, category) {
    el.classList.add("dz-toast", category);
    if (!el.querySelector(".dz-toast-x")) {
      var x = document.createElement("button");
      x.type = "button";
      x.className = "dz-toast-x";
      x.setAttribute("aria-label", "Dismiss");
      x.innerHTML = "&times;";
      x.addEventListener("click", function () { dismiss(el); });
      el.appendChild(x);
    }
    stack().appendChild(el);
    var timer = setTimeout(function () { dismiss(el); }, LIFE[category] || LIFE.info);
    // Reading a long message should not be a race against the timer.
    el.addEventListener("mouseenter", function () { clearTimeout(timer); });
    el.addEventListener("mouseleave", function () {
      timer = setTimeout(function () { dismiss(el); }, 1800);
    });
    return el;
  }

  window.dzToast = function (message, category) {
    if (!message) return null;
    category = ICONS[category] ? category : "info";
    var el = document.createElement("div");
    el.innerHTML = '<i class="fa ' + ICONS[category] + '"></i><div class="dz-toast-body"></div>';
    // textContent, not innerHTML: a notification usually carries a name, a
    // filename or an error string that came from somewhere else.
    el.querySelector(".dz-toast-body").textContent = String(message);
    return mount(el, category);
  };

  // ---- adopt whatever the webserver rendered ------------------------------
  var CATEGORY = [
    ["danger", /alert-danger|alert-error|toast-danger|is-danger|(^|[\s-])(error|danger)([\s-]|$)/i],
    ["warning", /alert-warning|toast-warning|is-warning|(^|[\s-])warning([\s-]|$)/i],
    ["success", /alert-success|toast-success|is-success|(^|[\s-])success([\s-]|$)/i]
  ];
  function categoryOf(el) {
    var name = typeof el.className === "string" ? el.className : "";
    for (var i = 0; i < CATEGORY.length; i++) {
      if (CATEGORY[i][1].test(name)) return CATEGORY[i][0];
    }
    return "info";
  }

  function looksLikeAlert(el) {
    if (!el || el.nodeType !== 1 || el.dataset.dzAdopted) return false;
    if (el.closest(".dz-toasts")) return false;
    var name = typeof el.className === "string" ? el.className : "";
    return /(^|\s)(alert|toast|notification|flash)([\s-]|$)/i.test(name);
  }

  function adopt(el) {
    if (!looksLikeAlert(el)) return;
    // A container of alerts is not itself an alert; take its children instead.
    if (el.querySelector(".alert, .toast, .notification")) return;
    if (!(el.textContent || "").trim()) return;
    el.dataset.dzAdopted = "1";
    var category = categoryOf(el);
    if (!el.querySelector(".fa")) {
      var icon = document.createElement("i");
      icon.className = "fa " + (ICONS[category] || ICONS.info);
      el.insertBefore(icon, el.firstChild);
    }
    mount(el, category);
  }

  function sweep(root) {
    var nodes = (root || document).querySelectorAll(
      '.alert, .toast, .notification, .flash, [class*="alert-"], [class*="toast-"]'
    );
    Array.prototype.forEach.call(nodes, adopt);
  }

  function start() {
    sweep(document);
    // Notifications that arrive after load - a framework toast, or anything a
    // page injects later - get picked up the same way.
    if (!window.MutationObserver) return;
    new MutationObserver(function (records) {
      records.forEach(function (record) {
        Array.prototype.forEach.call(record.addedNodes, function (node) {
          if (node.nodeType !== 1) return;
          adopt(node);
          sweep(node);
        });
      });
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
</script>
{% endraw %}
"""

BASE_CSS += NOTIFICATIONS


# Jinja macro library. Build templates as ``BASE_CSS + MACROS + markup`` so the
# macros are defined in the same template that calls them.
MACROS = """
{% macro picker(name, options, multiple=false, size=8, placeholder='Search...', allow_none=false, none_label='none') -%}
  <div class="dz-pick">
    {% if options|length > 8 %}
      <input class="dz-input dz-search" type="text" placeholder="{{ placeholder }}"
             oninput="(function(i){
               var s=i.parentNode.querySelector('select'); var q=i.value.toLowerCase(); var n=0;
               Array.prototype.forEach.call(s.querySelectorAll('option'),function(o){
                 var hit=o.text.toLowerCase().indexOf(q)>-1;
                 o.hidden=!hit && o.value!=='';
                 if(hit) n++;
               });
               Array.prototype.forEach.call(s.querySelectorAll('optgroup'),function(g){
                 g.hidden=!g.querySelector('option:not([hidden])');
               });
               var c=i.parentNode.querySelector('.dz-count');
               if(c) c.textContent=n+' of {{ options|length }} shown';
             })(this)" />
    {% endif %}
    <select class="dz-select" name="{{ name }}"
            {% if multiple %}multiple size="{{ size }}"{% endif %}>
      {% if allow_none and not multiple %}
        <option value="">&mdash; {{ none_label }} &mdash;</option>
      {% endif %}
      {% for group, items in options|groupby('group') %}
        <optgroup label="{{ group }}">
          {% for o in items %}
            <option value="{{ o.id }}" {% if o.selected %}selected{% endif %}>{{ o.name }}{% if o.warn %} !{% endif %}</option>
          {% endfor %}
        </optgroup>
      {% endfor %}
    </select>
    {% if options|length > 8 %}<div class="dz-count">{{ options|length }} available</div>{% endif %}
  </div>
{%- endmacro %}

{% macro stats(items) -%}
  <div class="dz-stats">
    {% for label, value in items %}
      <div class="dz-stat"><b>{{ value }}</b><span>{{ label }}</span></div>
    {% endfor %}
  </div>
{%- endmacro %}

{% macro msg(m) -%}
  <div class="dz-msg{% if m.old %} old{% endif %}">
    {% if m.avatar %}<img class="dz-av" src="{{ m.avatar }}" alt="" />{% else %}<div class="dz-av"></div>{% endif %}
    <div class="dz-body">
      <div class="dz-meta">
        <span class="dz-name">{{ m.author }}</span>
        {% if m.bot %}<span class="dz-botpill">BOT</span>{% endif %}
        <span class="dz-time">{{ m.timestamp }}</span>
        {% if m.pinned %}<span class="dz-tag">pinned</span>{% endif %}
        {% if m.old %}<span class="dz-tag warn">over 14 days</span>{% endif %}
      </div>
      {% if m.content %}<div class="dz-text">{{ m.content }}</div>{% endif %}
      {% if m.attachments %}<div class="dz-att">{{ m.attachments|join(', ') }}</div>{% endif %}
      {% for e in m.embeds %}
        <div class="dz-embed" style="border-left-color:{{ e.colour }};">
          {% if e.title %}<div class="et">{{ e.title }}</div>{% endif %}
          {% if e.description %}<div class="ed">{{ e.description }}</div>{% endif %}
          {% for f in e.fields %}<div class="efield"><b>{{ f.name }}</b>{{ f.value }}</div>{% endfor %}
          {% if e.footer %}<div class="ef">{{ e.footer }}</div>{% endif %}
        </div>
      {% endfor %}
    </div>
  </div>
{%- endmacro %}

{% macro msglist(messages, empty='Nothing matches.') -%}
  {% if messages %}
    <div class="dz-msgs">{% for m in messages %}{{ msg(m) }}{% endfor %}</div>
  {% else %}
    <p class="dz-empty">{{ empty }}</p>
  {% endif %}
{%- endmacro %}

{% macro confirm(label, action, question, cls='danger', icon='fa-trash-o') -%}
  <button class="dz-btn {{ cls }}" name="action" value="{{ action }}"
          onclick="return confirm('{{ question }}');">
    <i class="fa {{ icon }}"></i> {{ label }}
  </button>
{%- endmacro %}
"""
