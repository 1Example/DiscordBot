"""Generate LevelUp Image

Args:
    background_bytes (t.Optional[bytes], optional): The background image as bytes. Defaults to None.
    avatar_bytes (t.Optional[bytes], optional): The avatar image as bytes. Defaults to None.
    avatar_frame (t.Optional[bytes], optional): The member's avatar decoration. Defaults to None.
    username (str, optional): Shown above the level. Defaults to "".
    level (t.Optional[int], optional): The level number. Defaults to 1.
    color (t.Optional[t.Tuple[int, int, int]], optional): The color of the level text as a tuple of RGB values. Defaults to None.
    font (t.Optional[t.Union[str, Path]], optional): The path to the font file or the name of the font. Defaults to None.
    render_gif (t.Optional[bool], optional): Whether to render the image as a GIF. Defaults to False.
    debug (t.Optional[bool], optional): Whether to show the generated image for debugging purposes. Defaults to False.

Returns:
    bytes: The generated image as bytes.
"""

import logging
import typing as t
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageSequence, UnidentifiedImageError
from redbot.core.i18n import Translator

try:
    from . import imgtools
except ImportError:
    import imgtools

log = logging.getLogger("red.vrt.levelup.generator.levelalert")
_ = Translator("LevelUp", __file__)


def generate_level_img(
    background_bytes: t.Optional[t.Union[bytes, str]] = None,
    avatar_bytes: t.Optional[t.Union[bytes, str]] = None,
    avatar_frame: t.Optional[t.Union[bytes, str]] = None,
    username: str = "",
    level: int = 1,
    color: t.Optional[t.Tuple[int, int, int]] = None,
    font_path: t.Optional[t.Union[str, Path]] = None,
    render_gif: bool = False,
    debug: bool = False,
    **kwargs,
) -> t.Tuple[bytes, bool]:
    if isinstance(background_bytes, str) and background_bytes.startswith("http"):
        log.debug("Background image is a URL, attempting to download")
        background_bytes = imgtools.download_image(background_bytes)

    if isinstance(avatar_bytes, str) and avatar_bytes.startswith("http"):
        log.debug("Avatar image is a URL, attempting to download")
        avatar_bytes = imgtools.download_image(avatar_bytes)

    if isinstance(avatar_frame, str) and avatar_frame.startswith("http"):
        log.debug("Avatar frame is a URL, attempting to download")
        avatar_frame = imgtools.download_image(avatar_frame)

    if background_bytes:
        try:
            card = Image.open(BytesIO(background_bytes))
        except UnidentifiedImageError as e:
            log.error("Error opening background image", exc_info=e)
            card = imgtools.get_random_background()
    else:
        card = imgtools.get_random_background()
    pfp = imgtools.open_avatar(avatar_bytes)

    pfp_animated = getattr(pfp, "is_animated", False)
    bg_animated = getattr(card, "is_animated", False)
    pfp_frames = getattr(pfp, "n_frames", 1) if pfp_animated else 1
    bg_frames = getattr(card, "n_frames", 1) if bg_animated else 1

    width, height = 480, 132
    desired_card_size = (width, height)
    pad = 12
    pfp_size = height - pad * 2

    # ---------------- Avatar decoration ----------------
    # Decoded once, forward, at the size it is drawn: seeking an APNG back and
    # forth composites each frame onto whatever the last seek left behind.
    frame_pad = int(pfp_size * 0.1) if avatar_frame else 0
    sprite_size = pfp_size + frame_pad * 2
    deco_sprites: t.List[Image.Image] = []
    deco_duration = 0
    if avatar_frame:
        try:
            source = Image.open(BytesIO(avatar_frame))
            durations = []
            for frame in ImageSequence.Iterator(source):
                frame.load()
                if duration := frame.info.get("duration"):
                    durations.append(duration)
                deco_sprites.append(
                    frame.convert("RGBA").resize((sprite_size, sprite_size), Image.Resampling.LANCZOS)
                )
            if durations:
                deco_duration = sum(durations) // len(durations)
        except (ValueError, UnidentifiedImageError, OSError) as e:
            deco_sprites = []
            log.error("Failed to read avatar decoration", exc_info=e)
    if not deco_sprites:
        frame_pad = 0
        sprite_size = pfp_size
    pfp_paste = (pad - frame_pad, pad - frame_pad)

    # ---------------- Fonts ----------------
    font_path = font_path or imgtools.DEFAULT_FONT
    if isinstance(font_path, str):
        font_path = Path(font_path)
    if not font_path.exists():  # Hosted api specified a font that doesn't exist on the server
        if (imgtools.DEFAULT_FONTS / font_path.name).exists():
            font_path = imgtools.DEFAULT_FONTS / font_path.name
        else:
            font_path = imgtools.DEFAULT_FONT
    font_path = str(font_path)
    label_path = imgtools.DEFAULT_FONTS / "Roboto.ttf"
    label_path = str(label_path) if label_path.exists() else font_path

    accent = tuple(color or (88, 139, 255))[:3]

    # ---------------- The pane the text sits on ----------------
    # On its own layer, and it matters: text_halo blurs a layer's alpha and
    # doubles it to put a soft edge behind text. Drawn together with the text,
    # the pane's own 122 came back as 244 across the whole rectangle and the
    # glass rendered as a near-solid slab - 4% of the background showing.
    panel_left = pad + pfp_size + 18
    pane = Image.new("RGBA", desired_card_size, (0, 0, 0, 0))
    pane_draw = ImageDraw.Draw(pane)
    pane_draw.rectangle(
        (panel_left, pad, width - pad, height - pad),
        fill=(9, 11, 18, 72),
        outline=(255, 255, 255, 58),
        width=1,
    )
    pane_draw.line(
        (panel_left + 1, pad + 1, width - pad - 1, pad + 1), fill=(255, 255, 255, 78), width=1
    )

    ink = Image.new("RGBA", desired_card_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(ink)

    text_x = panel_left + 18
    text_right = width - pad - 16
    stroke = {"stroke_width": 1, "stroke_fill": (0, 0, 0, 205)}

    eyebrow = (username or _("Level up")).upper()
    eyebrow_font = ImageFont.truetype(label_path, 16)
    while eyebrow_font.getlength(eyebrow) > (text_right - text_x) and eyebrow_font.size > 9:
        eyebrow_font = ImageFont.truetype(label_path, eyebrow_font.size - 1)
    cursor = text_x
    for char in eyebrow:
        draw.text((cursor, pad + 16), char, font=eyebrow_font, fill=(214, 220, 235), **stroke)
        cursor += eyebrow_font.getlength(char) + 1.4

    hero = _("LEVEL {}").format(level)
    hero_font = ImageFont.truetype(font_path, 56)
    while hero_font.getlength(hero) > (text_right - text_x) and hero_font.size > 14:
        hero_font = ImageFont.truetype(font_path, hero_font.size - 1)
    draw.text((text_x, pad + 42), hero, font=hero_font, fill=accent, stroke_width=2,
              stroke_fill=(0, 0, 0, 205))

    # Pane at the bottom, then the halo, then the text itself.
    overlay = Image.alpha_composite(
        Image.alpha_composite(pane, imgtools.text_halo(ink)), ink
    )

    # ---------------- Frames ----------------
    def background_frame(index: int) -> Image.Image:
        if bg_animated:
            card.seek(index % bg_frames)
        return imgtools.fit_aspect_ratio(card.convert("RGBA"), desired_card_size)

    if not pfp_animated and pfp.mode != "RGBA":
        pfp = pfp.convert("RGBA")

    def avatar_circle(index: int, method) -> Image.Image:
        source = pfp
        if pfp_animated:
            pfp.seek(index % pfp_frames)
            source = pfp.copy()
            if source.mode != "RGBA":
                source = source.convert("RGBA")
        return imgtools.make_profile_circle(
            source.resize((pfp_size, pfp_size), method), method=method
        )

    def compose(index: int, method) -> Image.Image:
        frame = background_frame(index)
        frame.alpha_composite(overlay)
        avatar = avatar_circle(index, method)
        if deco_sprites:
            sprite = Image.new("RGBA", (sprite_size, sprite_size), (0, 0, 0, 0))
            sprite.paste(avatar, (frame_pad, frame_pad), avatar)
            avatar = Image.alpha_composite(sprite, deco_sprites[index % len(deco_sprites)])
        frame.paste(avatar, pfp_paste, avatar)
        return frame

    frame_count = max(pfp_frames, bg_frames, len(deco_sprites) or 1)
    if not render_gif or frame_count == 1:
        finished = compose(0, Image.Resampling.LANCZOS)
        if debug:
            finished.show(title="LevelUp Image")
        buffer = BytesIO()
        finished.save(buffer, format="WEBP")
        finished.close()
        return buffer.getvalue(), False

    avg_duration = 0
    for source, animated in ((pfp, pfp_animated), (card, bg_animated)):
        if animated and not avg_duration:
            avg_duration = imgtools.get_avg_duration(source)
    avg_duration = avg_duration or deco_duration or 60
    frame_count = min(frame_count, 50)
    log.debug(f"Rendering {frame_count} frames at {avg_duration}ms")

    frames = [compose(index, Image.Resampling.LANCZOS) for index in range(frame_count)]
    buffer = BytesIO()
    # WEBP rather than GIF: 256 palette entries cannot carry a photograph, an
    # avatar and a decoration without posterising all three.
    frames[0].save(
        buffer,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=avg_duration,
        loop=0,
        quality=86,
        method=4,
        minimize_size=True,
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
    res, animated = generate_level_img(
        background_bytes=test_banner,
        avatar_bytes=test_avatar,
        username="Vertyco",
        level=10,
        debug=True,
        render_gif=True,
    )
    result_path = imgtools.ASSETS / "tests" / "level.gif"
    result_path.write_bytes(res)
