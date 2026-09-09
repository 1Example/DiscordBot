import importlib.util
import logging
import sys
import typing as t
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, UnidentifiedImageError
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import humanize_number

try:
    # Loaded from cog
    from .. import imgtools
    from ..pilmojisrc.core import Pilmoji
except ImportError:
    # Running in vscode "Run Python File in Terminal"
    # Add parent directory to sys.path to enable imports
    parent_dir = Path(__file__).parent.parent
    sys.path.insert(0, str(parent_dir))

    # Import imgtools directly
    imgtools_path = parent_dir / "imgtools.py"
    if imgtools_path.exists():
        spec = importlib.util.spec_from_file_location("imgtools", imgtools_path)
        imgtools = importlib.util.module_from_spec(spec)
        sys.modules["imgtools"] = imgtools
        spec.loader.exec_module(imgtools)
    else:
        raise ImportError(f"Could not find imgtools at {imgtools_path}")

    # Set up pilmojisrc as a package
    pilmoji_dir = parent_dir / "pilmojisrc"
    if not pilmoji_dir.exists():
        raise ImportError(f"Could not find pilmojisrc directory at {pilmoji_dir}")

    # Create and register the pilmojisrc package
    pilmojisrc_init = pilmoji_dir / "__init__.py"
    if pilmojisrc_init.exists():
        spec = importlib.util.spec_from_file_location("pilmojisrc", pilmojisrc_init)
        pilmojisrc = importlib.util.module_from_spec(spec)
        sys.modules["pilmojisrc"] = pilmojisrc
        spec.loader.exec_module(pilmojisrc)
    else:
        # Create an empty module if __init__.py doesn't exist
        pilmojisrc = type(sys)("pilmojisrc")
        sys.modules["pilmojisrc"] = pilmojisrc

    # Import helpers module first (since core depends on it)
    helpers_path = pilmoji_dir / "helpers.py"
    if helpers_path.exists():
        spec = importlib.util.spec_from_file_location("pilmojisrc.helpers", helpers_path)
        helpers = importlib.util.module_from_spec(spec)
        setattr(pilmojisrc, "helpers", helpers)
        sys.modules["pilmojisrc.helpers"] = helpers
        spec.loader.exec_module(helpers)
    else:
        raise ImportError(f"Could not find helpers module at {helpers_path}")

    # Now import core module
    core_path = pilmoji_dir / "core.py"
    if core_path.exists():
        spec = importlib.util.spec_from_file_location("pilmojisrc.core", core_path)
        core = importlib.util.module_from_spec(spec)
        setattr(pilmojisrc, "core", core)
        sys.modules["pilmojisrc.core"] = core
        spec.loader.exec_module(core)
        Pilmoji = core.Pilmoji
    else:
        raise ImportError(f"Could not find core module at {core_path}")


log = logging.getLogger("red.vrt.levelup.generator.styles.default")
_ = Translator("LevelUp", __file__)


def compact(number: int) -> str:
    """Abbreviate keeping a significant digit: 16.2M rather than 16M."""
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(number) >= size:
            scaled = number / size
            return f"{scaled:.1f}{suffix}" if abs(scaled) < 100 else f"{scaled:.0f}{suffix}"
    return humanize_number(number)


def generate_default_profile(
    background_bytes: t.Optional[t.Union[bytes, str]] = None,
    avatar_bytes: t.Optional[t.Union[bytes, str]] = None,
    username: str = "Spartan117",
    status: str = "online",
    level: int = 3,
    messages: int = 420,
    voicetime: int = 3600,
    stars: int = 69,
    prestige: int = 0,
    prestige_emoji: t.Optional[t.Union[bytes, str]] = None,
    balance: int = 0,
    currency_name: str = "Credits",
    previous_xp: int = 100,
    current_xp: int = 125,
    next_xp: int = 200,
    position: int = 3,
    role_icon: t.Optional[t.Union[bytes, str]] = None,
    avatar_frame: t.Optional[t.Union[bytes, str]] = None,
    blur: bool = False,
    base_color: t.Tuple[int, int, int] = (255, 255, 255),
    user_color: t.Optional[t.Tuple[int, int, int]] = None,
    stat_color: t.Optional[t.Tuple[int, int, int]] = None,
    level_bar_color: t.Optional[t.Tuple[int, int, int]] = None,
    font_path: t.Optional[t.Union[str, Path]] = None,
    render_gif: bool = False,
    debug: bool = False,
    reraise: bool = False,
    square: bool = False,
    **kwargs,
) -> t.Tuple[bytes, bool]:
    """
    Generate a full profile image with customizable parameters.

    The card draws its own ground; the member's background is not composited.
    It is rendered as a gif when the avatar or the avatar decoration is
    animated, and as a static webp otherwise.

    Args:
        background_bytes (t.Optional[bytes], optional): Accepted for signature compatibility
            with the other styles and ignored. Defaults to None.
        avatar (t.Optional[bytes], optional): The avatar image as bytes. Defaults to None.
        username (t.Optional[str], optional): The username. Defaults to "Spartan117".
        status (t.Optional[str], optional): The status. Defaults to "online".
        level (t.Optional[int], optional): The level. Defaults to 1.
        messages (t.Optional[int], optional): The number of messages. Defaults to 0.
        voicetime (t.Optional[int], optional): The voicetime. Defaults to 3600.
        stars (t.Optional[int], optional): The number of stars. Defaults to 0.
        prestige (t.Optional[int], optional): The prestige level. Defaults to 0.
        prestige_emoji (t.Optional[bytes], optional): The prestige emoji as bytes. Defaults to None.
        balance (t.Optional[int], optional): The balance. Defaults to 0.
        currency_name (t.Optional[str], optional): The name of the currency. Defaults to "Credits".
        previous_xp (t.Optional[int], optional): The previous XP. Defaults to 0.
        current_xp (t.Optional[int], optional): The current XP. Defaults to 0.
        next_xp (t.Optional[int], optional): The next XP. Defaults to 0.
        position (t.Optional[int], optional): The position. Defaults to 0.
        role_icon (t.Optional[bytes, str], optional): The role icon as bytes or url. Defaults to None.
        avatar_frame (t.Optional[bytes, str], optional): The member's avatar decoration as bytes or url. Defaults to None.
        blur (t.Optional[bool], optional): Whether to blur the box behind the stats. Defaults to False.
        user_color (t.Optional[t.Tuple[int, int, int]], optional): The color for the user. Defaults to None.
        base_color (t.Optional[t.Tuple[int, int, int]], optional): The base color. Defaults to None.
        stat_color (t.Optional[t.Tuple[int, int, int]], optional): The color for the stats. Defaults to None.
        level_bar_color (t.Optional[t.Tuple[int, int, int]], optional): The color for the level bar. Defaults to None.
        font_path (t.Optional[t.Union[str, Path], optional): The path to the font file. Defaults to None.
        render_gif (t.Optional[bool], optional): Whether to render as gif if profile or background is one. Defaults to False.
        debug (t.Optional[bool], optional): Whether to raise any errors rather than suppressing. Defaults to False.
        reraise (t.Optional[bool], optional): Whether to raise any errors rather than suppressing. Defaults to False.
        square (t.Optional[bool], optional): Whether to render the profile as a square. Defaults to False.
        **kwargs: Additional keyword arguments.

    Returns:
        t.Tuple[bytes, bool]: The generated full profile image as bytes, and whether the image is animated.
    """
    user_color = user_color or base_color
    stat_color = stat_color or base_color
    level_bar_color = level_bar_color or base_color

    if isinstance(avatar_bytes, str) and avatar_bytes.startswith("http"):
        log.debug("Avatar image is a URL, attempting to download")
        avatar_bytes = imgtools.download_image(avatar_bytes)

    if isinstance(prestige_emoji, str) and prestige_emoji.startswith("http"):
        log.debug("Prestige emoji is a URL, attempting to download")
        prestige_emoji_bytes = imgtools.download_image(prestige_emoji)
    else:
        prestige_emoji_bytes = prestige_emoji

    if isinstance(role_icon, str) and role_icon.startswith("http"):
        log.debug("Role icon is a URL, attempting to download")
        role_icon_bytes = imgtools.download_image(role_icon)
    else:
        role_icon_bytes = role_icon

    if isinstance(avatar_frame, str) and avatar_frame.startswith("http"):
        log.debug("Avatar frame is a URL, attempting to download")
        avatar_frame_bytes = imgtools.download_image(avatar_frame)
    else:
        avatar_frame_bytes = avatar_frame

    # background_bytes and blur are accepted for signature compatibility with
    # the other styles and deliberately unused: this card draws its own ground.
    # Compositing the member's photo under the panels only ever showed up as a
    # ghost of it smeared across the card.
    pfp = imgtools.open_avatar(avatar_bytes)
    pfp_animated = getattr(pfp, "is_animated", False)
    pfp_frames = getattr(pfp, "n_frames", 1) if pfp_animated else 1

    # Setup
    width, height = (450, 450) if square else (1050, 450)
    desired_card_size = (width, height)

    if square:
        pad = 20
        head_top, head_bottom = 16, 120
        pfp_size = 76
        name_pt, sub_pt = 30, 15
        chip_h = 22
        hero_level = False
        cap_y, bar_y, bar_h = 130, 150, 9
        tiles_top, tile_h, tile_gap, tile_cols = 174, 58, 6, 2
        panel_radius, tile_radius = 14, 12
    else:
        pad = 34
        head_top, head_bottom = 26, 156
        pfp_size = 104
        name_pt, sub_pt = 48, 19
        chip_h = 28
        hero_level = True
        cap_y, bar_y, bar_h = 172, 196, 12
        tiles_top, tile_h, tile_gap, tile_cols = 226, 96, 12, 4
        panel_radius, tile_radius = 18, 16

    left = pad
    right = width - pad
    inner_w = right - left
    circle_x = left + (head_bottom - head_top - pfp_size) // 2
    circle_y = head_top + (head_bottom - head_top - pfp_size) // 2
    desired_pfp_size = (pfp_size, pfp_size)
    pfp_cx = circle_x + pfp_size / 2
    pfp_cy = circle_y + pfp_size / 2

    # ---------------- Avatar decoration ----------------
    # Discord ships decorations as a 96px box with the avatar filling the
    # middle 80, so the art is 1.2x the avatar and concentric with it.
    # Held open rather than converted up front: most decorations are animated
    # PNGs, and converting one collapses it to its first frame.
    deco_src: t.Optional[Image.Image] = None
    if avatar_frame_bytes:
        try:
            deco_src = Image.open(BytesIO(avatar_frame_bytes))
        except (ValueError, UnidentifiedImageError) as e:
            deco_src = None
            if reraise:
                raise e
            log.error(f"Failed to open avatar decoration for {username}", exc_info=e)
    deco_animated = getattr(deco_src, "is_animated", False)
    deco_frames = getattr(deco_src, "n_frames", 1) if deco_animated else 1
    frame_pad = int(pfp_size * 0.1) if deco_src is not None else 0
    pfp_paste = (circle_x - frame_pad, circle_y - frame_pad)

    def dress_avatar(circle_img: Image.Image, index: int = 0) -> Image.Image:
        """The circular avatar with one frame of its decoration over it."""
        if deco_src is None:
            return circle_img
        if deco_animated:
            deco_src.seek(index % deco_frames)
        size = circle_img.width + frame_pad * 2
        sprite = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        sprite.paste(circle_img, (frame_pad, frame_pad), circle_img)
        deco = deco_src.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        return Image.alpha_composite(sprite, deco)

    # ---------------- Fonts ----------------
    font_path = font_path or imgtools.DEFAULT_FONT
    if isinstance(font_path, str):
        font_path = Path(font_path)
    if not font_path.exists():
        # Hosted api on another server? Check if we have it
        if (imgtools.DEFAULT_FONTS / font_path.name).exists():
            font_path = imgtools.DEFAULT_FONTS / font_path.name
        else:
            font_path = imgtools.DEFAULT_FONT
    # Convert back to string
    font_path = str(font_path)

    # A display face and a text face. The member picks the display face; the
    # small tracked labels want something plain and lowercase-capable, and a
    # single face doing both jobs is most of why the old card read as flat.
    label_path = imgtools.DEFAULT_FONTS / "Roboto.ttf"
    label_path = str(label_path) if label_path.exists() else font_path

    def sized(points: float, path: t.Optional[str] = None) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(path or font_path, max(8, int(points)))

    def fitted(text: str, points: float, limit: float, path: t.Optional[str] = None) -> ImageFont.FreeTypeFont:
        """The largest size at or below `points` keeping `text` inside `limit`."""
        points = int(points)
        font = sized(points, path)
        while points > 9 and font.getlength(text) > limit:
            points -= 1
            font = sized(points, path)
        return font

    def tracked_width(text: str, font: ImageFont.FreeTypeFont, extra: float) -> float:
        if not text:
            return 0.0
        return sum(font.getlength(c) for c in text) + extra * (len(text) - 1)

    def tracked(xy, text: str, font: ImageFont.FreeTypeFont, fill, extra: float) -> None:
        """Uppercase labels need air between the letters to read as labels."""
        x, y = xy
        for char in text:
            draw.text((x, y), char, font=font, fill=fill)
            x += font.getlength(char) + extra

    # ---------------- Palette ----------------
    accent = tuple(user_color[:3])
    figure = tuple(stat_color[:3])
    bar_color = tuple(level_bar_color[:3])
    # Every colour falls back to base_color, which is plain white for a member
    # with no coloured role. White accents leave the card greyscale, so the
    # decoration borrows a default rather than inheriting the fallback.
    decor = accent if min(accent) < 210 else (88, 139, 255)
    if min(bar_color) >= 210:
        bar_color = decor
    label_ink = (*figure, 150)
    faint_ink = (*figure, 112)
    # A near black pulled a little toward the accent, so the card feels lit by
    # the same colour the accents are.
    ground = (max(9, accent[0] // 11), max(11, accent[1] // 11), max(17, accent[2] // 10))
    span = max(next_xp - previous_xp, 1)
    progress = min(max((current_xp - previous_xp) / span, 0.0), 1.0)

    # ---------------- Ground ----------------
    # RGB canvas: see the module note about ImageDraw and alpha. The card gets
    # its alpha in one go once everything is drawn.
    stats = Image.new("RGB", desired_card_size, ground)

    # A slow diagonal sheen. With no photo behind it the ground would otherwise
    # be a dead flat rectangle, and the panels would have nothing to sit on.
    sheen = Image.new("L", (64, 28))
    sheen_px = sheen.load()
    for sx in range(64):
        for sy in range(28):
            ramp = 1.0 - ((sx / 63) * 0.62 + (sy / 27) * 0.38)
            sheen_px[sx, sy] = int(max(0.0, ramp) ** 1.7 * 52)
    stats.paste(
        tuple(min(255, c + 46) for c in ground),
        mask=sheen.resize(desired_card_size, Image.Resampling.BICUBIC),
    )

    vignette = Image.new("L", desired_card_size, 0)
    ImageDraw.Draw(vignette).ellipse(
        (-width * 0.18, -height * 0.45, width * 1.18, height * 1.45),
        fill=255,
    )
    vignette = vignette.filter(ImageFilter.GaussianBlur(width / 16))
    stats.paste((0, 0, 0), mask=Image.eval(vignette, lambda v: (255 - v) * 3 // 5))

    # A wash of the accent behind the avatar, so the header has a light source.
    glow = Image.new("L", desired_card_size, 0)
    glow_r = pfp_size * 1.9
    ImageDraw.Draw(glow).ellipse(
        (pfp_cx - glow_r, pfp_cy - glow_r, pfp_cx + glow_r, pfp_cy + glow_r),
        fill=64,
    )
    stats.paste(decor, mask=glow.filter(ImageFilter.GaussianBlur(width / 22)))

    # ---------------- Panels ----------------
    tile_w = (inner_w - tile_gap * (tile_cols - 1)) / tile_cols
    panels = [((left, head_top, right, head_bottom), panel_radius)]
    for index in range(8):
        row, col = divmod(index, tile_cols)
        tx = left + col * (tile_w + tile_gap)
        ty = tiles_top + row * (tile_h + tile_gap)
        panels.append(((tx, ty, tx + tile_w, ty + tile_h), tile_radius))

    # One blur for every shadow rather than one per panel.
    shadow = Image.new("L", desired_card_size, 0)
    shadow_draw = ImageDraw.Draw(shadow)
    for (x0, y0, x1, y1), radius in panels:
        shadow_draw.rounded_rectangle((x0, y0 + 5, x1, y1 + 8), radius=radius, fill=150)
    stats.paste((0, 0, 0), mask=shadow.filter(ImageFilter.GaussianBlur(8)))

    draw = ImageDraw.Draw(stats, "RGBA")
    for box, radius in panels:
        draw.rounded_rectangle(box, radius=radius, fill=(255, 255, 255, 20), outline=(255, 255, 255, 36), width=1)
        x0, y0, x1, _bottom = box
        draw.line((x0 + radius, y0 + 1, x1 - radius, y0 + 1), fill=(255, 255, 255, 58), width=1)

    # Hairlines raked across the header, clipped to its rounded corners.
    rake = Image.new("L", desired_card_size, 0)
    rake_draw = ImageDraw.Draw(rake)
    for offset in range(int(width * 0.40), width + height, 46):
        rake_draw.line(
            (offset, head_top - 4, offset - (head_bottom - head_top) - 8, head_bottom + 4),
            fill=20,
            width=1,
        )
    header_mask = Image.new("L", desired_card_size, 0)
    ImageDraw.Draw(header_mask).rounded_rectangle(
        (left, head_top, right, head_bottom), radius=panel_radius, fill=255
    )
    stats.paste((255, 255, 255), mask=Image.composite(rake, Image.new("L", desired_card_size, 0), header_mask))

    # A ring around the avatar, unless the member brought their own frame.
    if deco_src is None:
        ring_w = 3 if square else 4
        ring_pad = ring_w / 2 + 2
        draw.ellipse(
            (
                circle_x - ring_pad,
                circle_y - ring_pad,
                circle_x + pfp_size + ring_pad,
                circle_y + pfp_size + ring_pad,
            ),
            outline=decor,
            width=ring_w,
        )

    # ---------------- Header: identity ----------------
    text_x = circle_x + pfp_size + (26 if square else 42)
    text_right = right - 18

    if hero_level:
        hero_label_font = sized(16, label_path)
        hero_font = sized(56)
        hero_value = humanize_number(level)
        hero_w = max(tracked_width(_("LEVEL"), hero_label_font, 2.4), hero_font.getlength(hero_value))
        text_right = right - 18 - hero_w - 30
        hero_x = right - 18 - hero_w
        tracked((hero_x, head_top + 24), _("LEVEL"), hero_label_font, label_ink, 2.4)
        draw.text((hero_x, head_top + 42), hero_value, fill=accent, font=hero_font)

    role_slot = 0 if square else 40
    name_font = fitted(username, name_pt, max(60, text_right - text_x - role_slot))
    name_y = head_top + (16 if square else 16)
    with Pilmoji(stats) as pilmoji:
        pilmoji.text(xy=(text_x, name_y), text=username, fill=figure, font=name_font)
    # The role icon reads as a badge on the name, which is what it is.
    if role_icon_bytes and not square:
        try:
            badge = Image.open(BytesIO(role_icon_bytes)).resize((32, 32), Image.Resampling.LANCZOS)
            stats.paste(badge, (int(text_x + name_font.getlength(username) + 12), name_y + 14), badge)
        except (ValueError, UnidentifiedImageError) as e:
            if reraise:
                raise e
            err = (
                f"Failed to paste role icon image for {username}"
                if isinstance(role_icon, bytes)
                else f"Failed to paste role icon image for {username}: {role_icon}"
            )
            log.error(err, exc_info=e)

    # ---------------- Header: chips ----------------
    chip_y = head_bottom - chip_h - (14 if square else 20)
    chip_font = sized(sub_pt, label_path)
    chip_x = text_x

    status_color = {
        "online": (67, 181, 129),
        "idle": (250, 168, 26),
        "dnd": (237, 66, 69),
        "streaming": (145, 71, 255),
        "offline": (128, 132, 142),
    }.get(status, (128, 132, 142))
    dot_r = 5 if square else 6
    status_text = status.upper() if status != "dnd" else _("DO NOT DISTURB")
    chip_w = dot_r * 2 + 10 + tracked_width(status_text, chip_font, 1.6) + 30
    draw.rounded_rectangle(
        (chip_x, chip_y, chip_x + chip_w, chip_y + chip_h),
        radius=chip_h // 2,
        fill=(*status_color, 46),
        outline=(*status_color, 150),
        width=1,
    )
    dot_cy = chip_y + chip_h / 2
    draw.ellipse(
        (chip_x + 15 - dot_r, dot_cy - dot_r, chip_x + 15 + dot_r, dot_cy + dot_r),
        fill=status_color,
    )
    tracked(
        (chip_x + 15 + dot_r + 8, chip_y + (chip_h - sub_pt) / 2 - 2),
        status_text,
        chip_font,
        (*figure, 220),
        1.6,
    )
    chip_x += chip_w + 10

    if prestige:
        prestige_text = _("PRESTIGE {}").format(humanize_number(prestige))
        emoji_slot = (chip_h - 8) if prestige_emoji_bytes else 0
        chip_w = tracked_width(prestige_text, chip_font, 1.6) + 30 + (emoji_slot + 8 if emoji_slot else 0)
        if chip_x + chip_w < text_right:
            draw.rounded_rectangle(
                (chip_x, chip_y, chip_x + chip_w, chip_y + chip_h),
                radius=chip_h // 2,
                fill=(*accent, 40),
                outline=(*accent, 150),
                width=1,
            )
            cursor = chip_x + 15
            if emoji_slot:
                try:
                    emoji = Image.open(BytesIO(prestige_emoji_bytes)).resize(
                        (emoji_slot, emoji_slot), Image.Resampling.LANCZOS
                    )
                    stats.paste(emoji, (int(cursor), int(chip_y + 4)), emoji)
                    cursor += emoji_slot + 8
                except (ValueError, UnidentifiedImageError) as e:
                    if reraise:
                        raise e
                    log.error(f"Failed to paste prestige emoji for {username}", exc_info=e)
            tracked((cursor, chip_y + (chip_h - sub_pt) / 2 - 2), prestige_text, chip_font, (*figure, 220), 1.6)

    # ---------------- Experience ----------------
    cap_font = sized(sub_pt + (0 if square else 2), label_path)
    caption = _("{} / {} XP").format(
        humanize_number(current_xp - previous_xp), humanize_number(next_xp - previous_xp)
    )
    draw.text((left + 2, cap_y), caption, fill=label_ink, font=cap_font)
    tail = _("{}% · {} XP TO LEVEL {}").format(
        int(progress * 100),
        humanize_number(max(next_xp - current_xp, 0)),
        humanize_number(level + 1),
    )
    tail_font = cap_font
    while tail_font.getlength(tail) > inner_w - cap_font.getlength(caption) - 30 and tail_font.size > 10:
        tail_font = sized(tail_font.size - 1, label_path)
    draw.text((right - 2 - tail_font.getlength(tail), cap_y), tail, fill=faint_ink, font=tail_font)

    # Drawn here rather than with make_progress_bar so the fill can carry a
    # gradient and the empty track can be filled instead of outlined.
    supersample = 4
    bar_px = (int(inner_w) * supersample, bar_h * supersample)
    bar = Image.new("RGBA", bar_px, (0, 0, 0, 0))
    bar_draw = ImageDraw.Draw(bar)
    bar_draw.rounded_rectangle((0, 0, bar_px[0] - 1, bar_px[1] - 1), bar_px[1] // 2, fill=(255, 255, 255, 34))
    if progress > 0:
        mask = Image.new("L", bar_px, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, max(bar_px[1], int(bar_px[0] * progress)), bar_px[1] - 1),
            bar_px[1] // 2,
            fill=255,
        )
        ramp = Image.new("RGBA", (bar_px[0], 1))
        dark = tuple(int(c * 0.55) for c in bar_color)
        light = tuple(min(255, int(c + (255 - c) * 0.35)) for c in bar_color)
        for px in range(bar_px[0]):
            ratio = px / max(bar_px[0] - 1, 1)
            ramp.putpixel((px, 0), tuple(int(dark[i] + (light[i] - dark[i]) * ratio) for i in range(3)) + (255,))
        ramp = ramp.resize(bar_px)
        ramp.putalpha(mask)
        bar = Image.alpha_composite(bar, ramp)
    bar = bar.resize((int(inner_w), bar_h), Image.Resampling.LANCZOS)
    stats.paste(bar, (left, bar_y), bar)

    # ---------------- Stat tiles ----------------
    voice_text = imgtools.abbreviate_time(voicetime) if voicetime else "0m"
    medal = {1: (255, 197, 66), 2: (196, 202, 212), 3: (198, 128, 74)}.get(position)
    cells = [
        (_("LEVEL"), humanize_number(level), None, None, accent),
        (_("RANK"), f"#{humanize_number(position)}", None, None, medal or figure),
        (_("TOTAL XP"), compact(current_xp), None, None, figure),
        (_("TO NEXT"), compact(max(next_xp - current_xp, 0)), _("LEVEL"), humanize_number(level + 1), figure),
        (_("MESSAGES"), compact(messages), None, None, figure),
        (_("VOICE TIME"), voice_text, None, None, figure),
        (_("STARS"), humanize_number(stars), None, None, figure),
        (_("BALANCE"), compact(balance), None, None, figure),
    ]
    if currency_name:
        cells[7] = (_("BALANCE"), compact(balance), None, currency_name.upper()[:9], figure)

    tile_label_pt = 12 if square else 15
    tile_value_pt = 26 if square else 42
    tile_sec_pt = 10 if square else 13
    tile_secv_pt = 15 if square else 21
    inset = 12 if square else 16
    for index, (label, value, sec_label, sec_value, colour) in enumerate(cells):
        row, col = divmod(index, tile_cols)
        tx = left + col * (tile_w + tile_gap)
        ty = tiles_top + row * (tile_h + tile_gap)

        tracked((tx + inset, ty + (9 if square else 13)), label, sized(tile_label_pt, label_path), label_ink, 1.3)

        value_y = ty + (24 if square else 34)
        reserved = 0.0
        if sec_label or sec_value:
            sec_l_font = sized(tile_sec_pt, label_path)
            sec_v_font = sized(tile_secv_pt)
            reserved = (
                max(
                    tracked_width(sec_label or "", sec_l_font, 1.2),
                    sec_v_font.getlength(sec_value or ""),
                )
                + 14
            )
            if sec_label:
                tracked(
                    (tx + tile_w - inset - tracked_width(sec_label, sec_l_font, 1.2), ty + (9 if square else 14)),
                    sec_label,
                    sec_l_font,
                    faint_ink,
                    1.2,
                )

        value_font = fitted(value, tile_value_pt, tile_w - inset * 2 - reserved)
        draw.text((tx + inset, value_y), value, fill=colour, font=value_font)

        if sec_value:
            # Sit the pair on one line: the fonts differ in size and in face, so
            # match where the digits end rather than where the boxes start.
            drop = value_font.getbbox("0")[3] - sec_v_font.getbbox("0")[3]
            draw.text(
                (tx + tile_w - inset - sec_v_font.getlength(sec_value), value_y + drop),
                sec_value,
                fill=faint_ink,
                font=sec_v_font,
            )

    stats = stats.convert("RGBA")

    # ---------------- Start finalizing the image ----------------
    card = imgtools.round_image_corners(stats, 45)
    if not pfp_animated and pfp.mode != "RGBA":
        log.debug(f"Converting pfp mode '{pfp.mode}' to RGBA")
        pfp = pfp.convert("RGBA")

    # Either the avatar or the decoration animating is enough to make this a
    # gif; whichever has more frames sets the length.
    frame_count = max(pfp_frames, deco_frames)

    def avatar_circle(index: int, method) -> Image.Image:
        """The avatar cropped to a circle, at one frame of its animation."""
        source = pfp
        if pfp_animated:
            pfp.seek(index % pfp_frames)
            source = pfp.copy()
            if source.mode != "RGBA":
                source = source.convert("RGBA")
        return imgtools.make_profile_circle(source.resize(desired_pfp_size, method), method=method)

    if not render_gif or frame_count == 1:
        sprite = dress_avatar(avatar_circle(0, Image.Resampling.LANCZOS))
        card.paste(sprite, pfp_paste, sprite)
        if debug:
            card.show()
        buffer = BytesIO()
        card.save(buffer, format="WEBP")
        card.close()
        return buffer.getvalue(), False

    # When both animate, the avatar sets the pace and the decoration is
    # sampled against it rather than the two being reconciled by their LCM,
    # which used to blow the frame count up to keep a background in step.
    avg_duration = imgtools.get_avg_duration(pfp if pfp_animated else deco_src) or 60
    log.debug(f"Rendering {frame_count} frames at {avg_duration}ms")
    frames: t.List[Image.Image] = []
    for index in range(frame_count):
        card_frame = card.copy()
        sprite = dress_avatar(avatar_circle(index, Image.Resampling.NEAREST), index)
        card_frame.paste(sprite, pfp_paste, sprite)
        frames.append(card_frame)

    buffer = BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=avg_duration,
        loop=0,
        quality=75,
        optimize=True,
    )
    buffer.seek(0)
    if debug:
        Image.open(buffer).show()
    return buffer.getvalue(), True


if __name__ == "__main__":
    # Setup console logging
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("PIL").setLevel(logging.INFO)

    test_avatar = (imgtools.ASSETS / "tests" / "tree.gif").read_bytes()
    test_icon = (imgtools.ASSETS / "tests" / "icon.png").read_bytes()
    font_path = imgtools.ASSETS / "fonts" / "BebasNeue.ttf"
    res, animated = generate_default_profile(
        avatar_bytes=test_avatar,
        username="Vertyco",
        status="online",
        level=999,
        messages=420,
        voicetime=399815,
        stars=693333,
        prestige=2,
        prestige_emoji=test_icon,
        balance=1000000,
        currency_name="Coinz 💰",
        previous_xp=1000,
        current_xp=1258,
        next_xp=5000,
        role_icon=test_icon,
        blur=True,
        font_path=font_path,
        render_gif=True,
        debug=True,
    )
    result_path = imgtools.ASSETS / "tests" / "result.gif"
    result_path.write_bytes(res)
