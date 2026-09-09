import importlib.util
import logging
import math
import sys
import typing as t
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageSequence, UnidentifiedImageError
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
    If the avatar is animated and not the background, the avatar will be rendered as a gif.
    If the background is animated and not the avatar, the background will be rendered as a gif.
    If both are animated, the avatar will be rendered as a gif and the background will be rendered as a static image.
    To optimize performance, the profile will be generated in 3 layers, the background, the avatar, and the stats.
    The stats layer will be generated as a separate image and then pasted onto the background.

    Args:
        background (t.Optional[bytes], optional): The background image as bytes. Defaults to None.
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

    if isinstance(background_bytes, str) and background_bytes.startswith("http"):
        log.debug("Background image is a URL, attempting to download")
        background_bytes = imgtools.download_image(background_bytes)

    if isinstance(avatar_bytes, str) and avatar_bytes.startswith("http"):
        log.debug("Avatar image is a URL, attempting to download")
        avatar_bytes = imgtools.download_image(avatar_bytes)

    if isinstance(prestige_emoji, str) and prestige_emoji.startswith("http"):
        log.debug("Prestige emoji is a URL, attempting to download")
        prestige_emoji = imgtools.download_image(prestige_emoji)

    if isinstance(role_icon, str) and role_icon.startswith("http"):
        log.debug("Role icon is a URL, attempting to download")
        role_icon_bytes = imgtools.download_image(role_icon)
    else:
        role_icon_bytes = role_icon

    if background_bytes:
        try:
            card = Image.open(BytesIO(background_bytes))
        except UnidentifiedImageError as e:
            if reraise:
                raise e
            log.error(
                f"Failed to open background image ({type(background_bytes)} - {len(background_bytes)})", exc_info=e
            )
            card = imgtools.get_random_background()
    else:
        card = imgtools.get_random_background()
    pfp = imgtools.open_avatar(avatar_bytes)

    pfp_animated = getattr(pfp, "is_animated", False)
    bg_animated = getattr(card, "is_animated", False)
    log.debug(f"PFP animated: {pfp_animated}, BG animated: {bg_animated}")

    # Setup
    default_fill = (0, 0, 0)  # Default fill color for text
    stroke_width = 2  # Width of the stroke around text

    if square:
        desired_card_size = (450, 450)
        # aspect_ratio = imgtools.calc_aspect_ratio(*desired_card_size)
        name_y = 35  # Upper bound of username placement
        stats_y = 160  # Upper bound of stats texts
        blur_edge = 450  # Left bound of blur edge
        bar_width = 550  # Length of level bar
        bar_height = 40  # Height of level bar
        bar_start = 475  # Left bound of level bar
        bar_top = 380  # Top bound of level bar
        stat_bottom = bar_top - 10  # Bottom bound of all stats
        stat_start = bar_start + 10  # Left bound of all stats
        stat_split = stat_start + 210  # Split between left and right stats
        stat_end = 990  # Right bound of all stats
        stat_offset = 45  # Offset between stats
        circle_x = 60  # Left bound of profile circle
        circle_y = 60  # Top bound of profile circle
        star_text_x = 910  # Left bound of star text
        star_text_y = 35  # Top bound of star text
        star_icon_x = 850  # Left bound of star icon
        star_icon_y = 35  # Top bound of star icon
    else:
        # Ensure the card is the correct size and aspect ratio
        desired_card_size = (1050, 450)
        # aspect_ratio = imgtools.calc_aspect_ratio(*desired_card_size)
        name_y = 35  # Upper bound of username placement
        stats_y = 160  # Upper bound of stats texts
        blur_edge = 450  # Left bound of blur edge
        bar_width = 550  # Length of level bar
        bar_height = 40  # Height of level bar
        bar_start = 475  # Left bound of level bar
        bar_top = 380  # Top bound of level bar
        stat_bottom = bar_top - 10  # Bottom bound of all stats
        stat_start = bar_start + 10  # Left bound of all stats
        stat_split = stat_start + 210  # Split between left and right stats
        stat_end = 990  # Right bound of all stats
        stat_offset = 45  # Offset between stats
        circle_x = 60  # Left bound of profile circle
        circle_y = 60  # Top bound of profile circle
        star_text_x = 910  # Left bound of star text
        star_text_y = 35  # Top bound of star text
        star_icon_x = 850  # Left bound of star icon
        star_icon_y = 35  # Top bound of star icon

    # Establish layer for all text and accents
    stats = Image.new("RGBA", desired_card_size, (0, 0, 0, 0))

    # Establish font
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

    def sized(points: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(font_path, points)

    def fitted(text: str, points: int, limit: float) -> ImageFont.FreeTypeFont:
        """The largest size at or below `points` keeping `text` inside `limit`."""
        font = sized(points)
        while points > 11 and font.getlength(text) > limit:
            points -= 1
            font = sized(points)
        return font

    accent = tuple(user_color[:3])
    muted = (*stat_color[:3], 195)
    progress = (current_xp - previous_xp) / (next_xp - previous_xp)

    # ---------------- Grade the whole card ----------------
    # A diagonal wash rather than a box: the background stays visible at the
    # top right and gets out of the way everywhere text lands.
    width, height = desired_card_size
    grade = Image.new("L", (width, height))
    grade_px = grade.load()
    for x in range(0, width, 2):
        for y in range(0, height, 2):
            # 0 at the top right corner, 1 at the bottom left
            t = ((width - x) / width) * 0.55 + (y / height) * 0.45
            # A mid alpha over a pale photo just reads as grey haze, so the
            # floor is high: the background becomes texture and atmosphere
            # rather than something the text has to compete with.
            value = 148 + int(min(1.0, t) ** 1.3 * 92)
            grade_px[x, y] = value
            if x + 1 < width:
                grade_px[x + 1, y] = value
            if y + 1 < height:
                grade_px[x, y + 1] = value
                if x + 1 < width:
                    grade_px[x + 1, y + 1] = value
    # Tinted a touch toward the accent so the card feels lit, not just dim.
    wash = Image.new(
        "RGBA",
        (width, height),
        (max(8, accent[0] // 12), max(10, accent[1] // 12), max(18, accent[2] // 10), 255),
    )
    wash.putalpha(grade)
    stats = Image.alpha_composite(stats, wash)
    draw = ImageDraw.Draw(stats)

    # ---------------- XP ring around the avatar ----------------
    ring_box = (circle_x - 22, circle_y - 22, circle_x + 352, circle_y + 352)
    draw.arc(ring_box, 0, 360, fill=(*stat_color[:3], 55), width=14)
    if progress > 0:
        draw.arc(ring_box, -90, -90 + int(360 * min(progress, 1.0)), fill=accent, width=14)

    # ---------------- Right column ----------------
    # Derived from the card, not hardcoded: the square variant is 450 wide,
    # where a column starting at 430 has negative width and PIL refuses to
    # draw it. Nothing passes square= today, but it should degrade rather
    # than raise if something starts to.
    beside_avatar = width >= 900
    col_x = 430 if beside_avatar else 30
    col_right = width - 45
    col_width = col_right - col_x

    # Name, with prestige beside it since it qualifies the name.
    name_font = fitted(username, 58, col_width - 70)
    name_y = 42 if beside_avatar else circle_y + 350
    with Pilmoji(stats) as pilmoji:
        pilmoji.text(
            xy=(col_x, name_y),
            text=username,
            fill=user_color,
            stroke_width=1,
            stroke_fill=default_fill,
            font=name_font,
        )
    if prestige and prestige_emoji_bytes:
        try:
            icon = Image.open(BytesIO(prestige_emoji_bytes)).resize((46, 46), Image.Resampling.LANCZOS)
            stats.paste(icon, (int(col_x + name_font.getlength(username) + 14), name_y + 8), icon)
        except (ValueError, UnidentifiedImageError) as e:
            if reraise:
                raise e
            log.error(f"Failed to paste prestige emoji for {username}", exc_info=e)

    # ---------------- Standing: rank, level, stars ----------------
    # Rank carries a colour in the top three, the way a leaderboard does.
    medal = {1: (255, 197, 66), 2: (196, 202, 212), 3: (198, 128, 74)}.get(position)
    pills = [
        (_("RANK"), f"#{humanize_number(position)}", medal or accent),
        (_("LEVEL"), humanize_number(level), accent),
        (_("STARS"), humanize_number(stars), (*stat_color[:3], 255)),
    ]
    pill_y = name_y + 74
    pill_h = 46
    pill_x = col_x
    label_font = sized(19)
    for label, value, colour in pills:
        value_font = sized(30)
        inner = label_font.getlength(label) + 10 + value_font.getlength(value)
        pill_w = inner + 46
        draw.rounded_rectangle(
            (pill_x, pill_y, pill_x + pill_w, pill_y + pill_h),
            radius=pill_h // 2,
            fill=(8, 10, 18, 160),
            outline=(*colour[:3], 150),
            width=2,
        )
        draw.text((pill_x + 22, pill_y + 15), label, fill=(*colour[:3], 210), font=label_font)
        draw.text(
            (pill_x + 22 + label_font.getlength(label) + 10, pill_y + 8),
            value,
            fill=colour[:3],
            font=value_font,
        )
        pill_x += pill_w + 12

    # ---------------- Stat tiles ----------------
    tiles = [
        (_("MESSAGES"), humanize_number(messages)),
        (_("VOICE"), imgtools.abbreviate_time(voicetime, short=True) if voicetime else "-"),
        (_("BALANCE"), f"{imgtools.abbreviate_number(balance)}"),
        (_("TOTAL XP"), imgtools.abbreviate_number(current_xp)),
    ]
    tile_y = pill_y + pill_h + 20
    tile_h = 82
    gap = 12
    tile_w = (col_width - gap * (len(tiles) - 1)) / len(tiles)
    for index, (label, value) in enumerate(tiles):
        tx = col_x + index * (tile_w + gap)
        draw.rounded_rectangle(
            (tx, tile_y, tx + tile_w, tile_y + tile_h),
            radius=14,
            fill=(8, 10, 18, 170),
            outline=(255, 255, 255, 42),
            width=1,
        )
        draw.text((tx + 14, tile_y + 12), label, fill=muted, font=sized(19))
        draw.text(
            (tx + 14, tile_y + 33),
            value,
            fill=stat_color,
            font=fitted(value, 34, tile_w - 28),
        )

    # ---------------- Experience ----------------
    remaining = max(next_xp - current_xp, 0)
    bar_y = tile_y + tile_h + 26
    bar_h = 26
    level_bar = imgtools.make_progress_bar(int(col_width), bar_h, progress, level_bar_color)
    stats.paste(level_bar, (col_x, bar_y + 22), level_bar)
    small = sized(21)
    draw.text(
        (col_x, bar_y),
        _("{} / {} XP").format(
            humanize_number(current_xp - previous_xp), humanize_number(next_xp - previous_xp)
        ),
        fill=muted,
        font=small,
    )
    right_text = _("{} XP to level {}").format(humanize_number(remaining), humanize_number(level + 1))
    draw.text(
        (col_right - small.getlength(right_text), bar_y),
        right_text,
        fill=muted,
        font=small,
    )

    # ---------------- Profile Accents ----------------
    # Place status icon
    status_icon = imgtools.STATUS[status].resize((66, 66), Image.Resampling.LANCZOS)
    stats.paste(status_icon, (circle_x + 262, circle_y + 262), status_icon)
    # Paste role icon on top left of profile circle
    if role_icon_bytes:
        try:
            role_icon_img = Image.open(BytesIO(role_icon_bytes)).resize((70, 70), Image.Resampling.LANCZOS)
            stats.paste(role_icon_img, (10, 10), role_icon_img)
        except ValueError as e:
            if reraise:
                raise e
            err = (
                f"Failed to paste role icon image for {username}"
                if isinstance(role_icon, bytes)
                else f"Failed to paste role icon image for {username}: {role_icon}"
            )
            log.error(err, exc_info=e)

    # ---------------- Start finalizing the image ----------------
    # Resize the profile image
    desired_pfp_size = (330, 330)
    if not render_gif or (not pfp_animated and not bg_animated):
        if card.mode != "RGBA":
            log.debug(f"Converting card mode '{card.mode}' to RGBA")
            card = card.convert("RGBA")
        if pfp.mode != "RGBA":
            log.debug(f"Converting pfp mode '{pfp.mode}' to RGBA")
            pfp = pfp.convert("RGBA")
        card = imgtools.fit_aspect_ratio(card, desired_card_size)
        if blur:
            blur_section = imgtools.blur_section(card, (blur_edge, 0, card.width, card.height))
            # Paste onto the stats
            card.paste(blur_section, (blur_edge, 0), blur_section)
        card = imgtools.round_image_corners(card, 45)
        pfp = pfp.resize(desired_pfp_size, Image.Resampling.LANCZOS)
        # Crop the profile image into a circle
        pfp = imgtools.make_profile_circle(pfp)
        # Paste the items onto the card
        card.paste(stats, (0, 0), stats)
        card.paste(pfp, (circle_x, circle_y), pfp)
        if debug:
            card.show()
        buffer = BytesIO()
        card.save(buffer, format="WEBP")
        card.close()
        return buffer.getvalue(), False

    if pfp_animated and not bg_animated:
        if card.mode != "RGBA":
            log.debug(f"Converting card mode '{card.mode}' to RGBA")
            card = card.convert("RGBA")
        card = imgtools.fit_aspect_ratio(card, desired_card_size)
        if blur:
            blur_section = imgtools.blur_section(card, (blur_edge, 0, card.width, card.height))
            # Paste onto the stats
            card.paste(blur_section, (blur_edge, 0), blur_section)

        card.paste(stats, (0, 0), stats)

        avg_duration = imgtools.get_avg_duration(pfp)
        log.debug(f"Rendering pfp as gif with avg duration of {avg_duration}ms")
        frames: t.List[Image.Image] = []
        for frame in range(getattr(pfp, "n_frames", 1)):
            pfp.seek(frame)
            # Prepare copies of the card, stats, and pfp
            card_frame = card.copy()
            pfp_frame = pfp.copy()
            if pfp_frame.mode != "RGBA":
                pfp_frame = pfp_frame.convert("RGBA")
            # Resize the profile image for each frame
            pfp_frame = pfp_frame.resize(desired_pfp_size, Image.Resampling.NEAREST)
            # Crop the profile image into a circle
            pfp_frame = imgtools.make_profile_circle(pfp_frame, method=Image.Resampling.NEAREST)
            # Paste the profile image onto the card
            card_frame.paste(pfp_frame, (circle_x, circle_y), pfp_frame)
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
    elif bg_animated and not pfp_animated:
        avg_duration = imgtools.get_avg_duration(card)
        log.debug(f"Rendering card as gif with avg duration of {avg_duration}ms")
        frames: t.List[Image.Image] = []

        if pfp.mode != "RGBA":
            log.debug(f"Converting pfp mode '{pfp.mode}' to RGBA")
            pfp = pfp.convert("RGBA")
        pfp = pfp.resize(desired_pfp_size, Image.Resampling.LANCZOS)
        # Crop the profile image into a circle
        pfp = imgtools.make_profile_circle(pfp)
        for frame in range(getattr(card, "n_frames", 1)):
            card.seek(frame)
            # Prepare copies of the card and stats
            card_frame = card.copy()
            card_frame = imgtools.fit_aspect_ratio(card_frame, desired_card_size)
            if card_frame.mode != "RGBA":
                card_frame = card_frame.convert("RGBA")

            # Paste items onto the card
            if blur:
                blur_section = imgtools.blur_section(card_frame, (blur_edge, 0, card_frame.width, card_frame.height))
                card_frame.paste(blur_section, (blur_edge, 0), blur_section)

            card_frame.paste(pfp, (circle_x, circle_y), pfp)
            card_frame.paste(stats, (0, 0), stats)

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

    # If we're here, both the avatar and background are gifs
    # Figure out how to merge the two frame counts and durations together
    # Calculate frame durations based on the LCM
    pfp_duration = imgtools.get_avg_duration(pfp)  # example: 50ms
    card_duration = imgtools.get_avg_duration(card)  # example: 100ms
    log.debug(f"PFP duration: {pfp_duration}ms, Card duration: {card_duration}ms")
    # Figure out how to round the durations
    # Favor the card's duration time over the pfp
    # Round both durations to the nearest X ms based on what will get the closest to the LCM
    pfp_duration = round(card_duration, -1)  # Round to the nearest 10ms
    card_duration = round(card_duration, -1)  # Round to the nearest 10ms

    log.debug(f"Modified PFP duration: {pfp_duration}ms, Card duration: {card_duration}ms")
    combined_duration = math.lcm(pfp_duration, card_duration)  # example: 100ms would be the LCM of 50 and 100
    log.debug(f"Combined duration: {combined_duration}ms")
    # The combined duration should be no more than 20% offset from the image with the highest duration
    max_duration = max(pfp_duration, card_duration)
    if combined_duration > max_duration * 1.2:
        log.debug(f"Combined duration is more than 20% offset from the max duration ({max_duration}ms)")
        combined_duration = max_duration

    pfp_frame_count = getattr(pfp, "n_frames", 1)
    card_frame_count = getattr(card, "n_frames", 1)
    total_pfp_duration = pfp_frame_count * pfp_duration  # example: 2250ms
    total_card_duration = card_frame_count * card_duration  # example: 3300ms
    # Total duration for the combined animation cycle (LCM of 2250 and 3300)
    total_duration = math.lcm(total_pfp_duration, total_card_duration)  # example: 9900ms
    num_combined_frames = total_duration // combined_duration

    # The maximum frame count should be no more than 20% offset from the image with the highest frame count to avoid filesize bloat
    max_frame_count = max(pfp_frame_count, card_frame_count) * 1.2
    max_frame_count = min(round(max_frame_count), num_combined_frames)
    log.debug(f"Max frame count: {max_frame_count}")
    # Create a list to store the combined frames
    combined_frames = []
    for frame_num in range(max_frame_count):
        time = frame_num * combined_duration

        # Calculate the frame index for both the card and pfp
        card_frame_index = (time // card_duration) % card_frame_count
        pfp_frame_index = (time // pfp_duration) % pfp_frame_count

        # Get the frames for the card and pfp
        card_frame = ImageSequence.Iterator(card)[card_frame_index]
        pfp_frame = ImageSequence.Iterator(pfp)[pfp_frame_index]

        card_frame = imgtools.fit_aspect_ratio(card_frame, desired_card_size)
        if card_frame.mode != "RGBA":
            card_frame = card_frame.convert("RGBA")

        if blur:
            blur_section = imgtools.blur_section(card_frame, (blur_edge, 0, card_frame.width, card_frame.height))
            # Paste onto the stats
            card_frame.paste(blur_section, (blur_edge, 0), blur_section)
        if pfp_frame.mode != "RGBA":
            pfp_frame = pfp_frame.convert("RGBA")

        pfp_frame = pfp_frame.resize(desired_pfp_size, Image.Resampling.NEAREST)
        pfp_frame = imgtools.make_profile_circle(pfp_frame, method=Image.Resampling.NEAREST)

        card_frame.paste(pfp_frame, (circle_x, circle_y), pfp_frame)
        card_frame.paste(stats, (0, 0), stats)

        combined_frames.append(card_frame)

    buffer = BytesIO()
    combined_frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=combined_frames[1:],
        loop=0,
        duration=combined_duration,
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

    test_banner = (imgtools.ASSETS / "tests" / "banner3.gif").read_bytes()
    test_avatar = (imgtools.ASSETS / "tests" / "tree.gif").read_bytes()
    test_icon = (imgtools.ASSETS / "tests" / "icon.png").read_bytes()
    font_path = imgtools.ASSETS / "fonts" / "BebasNeue.ttf"
    res, animated = generate_default_profile(
        background_bytes=test_banner,
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
