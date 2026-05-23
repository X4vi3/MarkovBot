import io

from PIL import Image, ImageDraw, ImageFont

PHOTO_WIDTH = 600
BORDER = 3
PADDING = 40
BOTTOM_PAD = 60
FONT_SIZE_TITLE = 42
FONT_SIZE_SUB = 20

# Шрифты по приоритету: Times New Roman → DejaVu Serif → любой serif
_SERIF_FONTS = [
    # Windows
    "times.ttf", "timesbd.ttf", "Times New Roman.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf",
    # Fallback sans
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "arial.ttf",
]


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    for name in _SERIF_FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Переносит текст по пиксельной ширине, а не по символам."""
    words = text.split()
    if not words:
        return text
    lines: list[str] = []
    current_line = words[0]
    tmp = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(tmp)
    for word in words[1:]:
        test = current_line + " " + word
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current_line = test
        else:
            lines.append(current_line)
            current_line = word
    lines.append(current_line)
    return "\n".join(lines)


def make_demotivator(photo_bytes: bytes, title: str, subtitle: str = "") -> bytes | None:
    try:
        return _make_demotivator_impl(photo_bytes, title, subtitle)
    except Exception:
        return None


def _make_demotivator_impl(photo_bytes: bytes, title: str, subtitle: str = "") -> bytes:
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")

    ratio = PHOTO_WIDTH / img.width
    img = img.resize((PHOTO_WIDTH, int(img.height * ratio)), Image.LANCZOS)

    font_title = _get_font(FONT_SIZE_TITLE)
    font_sub = _get_font(FONT_SIZE_SUB)

    max_text_w = PHOTO_WIDTH + PADDING  # Текст может быть чуть шире фото, но в пределах
    title_wrapped = _wrap_text(title, font_title, max_text_w)
    subtitle_wrapped = _wrap_text(subtitle, font_sub, max_text_w + 20) if subtitle else ""

    tmp_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_bbox = tmp_draw.multiline_textbbox((0, 0), title_wrapped, font=font_title)
    title_w = title_bbox[2] - title_bbox[0]
    title_h = title_bbox[3] - title_bbox[1]
    sub_w = 0
    sub_h = 0
    if subtitle_wrapped:
        sub_bbox = tmp_draw.multiline_textbbox((0, 0), subtitle_wrapped, font=font_sub)
        sub_w = sub_bbox[2] - sub_bbox[0]
        sub_h = sub_bbox[3] - sub_bbox[1] + 10

    # Ширина холста — максимум из фото и текста
    content_w = max(PHOTO_WIDTH, title_w, sub_w)
    canvas_w = content_w + PADDING * 2
    canvas_h = img.height + PADDING * 2 + BOTTOM_PAD + title_h + sub_h + 20

    canvas = Image.new("RGB", (canvas_w, canvas_h), "black")

    photo_x = (canvas_w - img.width) // 2
    photo_y = PADDING
    canvas.paste(img, (photo_x, photo_y))

    draw = ImageDraw.Draw(canvas)
    draw.rectangle(
        [photo_x - BORDER, photo_y - BORDER,
         photo_x + img.width + BORDER, photo_y + img.height + BORDER],
        outline="white", width=BORDER,
    )

    text_y = photo_y + img.height + 25
    draw.multiline_text(
        (canvas_w // 2, text_y), title_wrapped,
        font=font_title, fill="white", anchor="ma", align="center",
    )

    if subtitle_wrapped:
        draw.multiline_text(
            (canvas_w // 2, text_y + title_h + 10), subtitle_wrapped,
            font=font_sub, fill="#CCCC00", anchor="ma", align="center",
        )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
