from io import BytesIO
from math import ceil, log

from PIL import Image, ImageDraw, ImageFont


TILE_SIZE = (640, 480)
CAPTION_HEIGHT = 24


def _caption_lines(draw, text, font, width, limit=2):
    """Wrap names to a bounded band, with an ellipsis when text is omitted."""
    remaining = ' '.join(text.split())
    lines = []
    while remaining and len(lines) < limit:
        end = len(remaining)
        suffix = '…' if len(lines) == limit - 1 else ''
        while end and draw.textlength(remaining[:end] + (suffix if end < len(remaining) else ''), font=font) > width:
            end -= 1
        if end == len(remaining):
            lines.append(remaining)
            break
        if suffix:
            lines.append(remaining[:end].rstrip() + suffix)
            break
        boundary = remaining.rfind(' ', 0, end + 1)
        end = boundary if boundary > 0 else max(1, end)
        lines.append(remaining[:end].rstrip())
        remaining = remaining[end:].lstrip()
    return lines


def gallery(images, *, caption_overlay=False):
    """Encode ordered (label, image) pairs; overlays reserve the first line for identity."""
    if not images:
        raise ValueError('Gallery requires at least one image')
    tiles = []
    for label, image in images:
        if image.width <= 0 or image.height <= 0:
            raise ValueError('Gallery images must have positive dimensions')
        scale = min(1, TILE_SIZE[0] / image.width, TILE_SIZE[1] / image.height)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        tiles.append((str(label), image.resize(size, Image.Resampling.LANCZOS)))
    width = max(image.width for _, image in tiles)
    image_height = max(image.height for _, image in tiles)
    height = image_height if caption_overlay else image_height + CAPTION_HEIGHT
    count = len(tiles)
    columns = min(range(1, count + 1), key=lambda cols: (
        abs(log(cols / ceil(count / cols))) + (cols * ceil(count / cols) - count) / count,
        cols * ceil(count / cols),
        -cols,
    ))
    rows = ceil(count / columns)
    grid = Image.new('RGB', (width * columns, height * rows), '#202020')
    draw = ImageDraw.Draw(grid)
    if caption_overlay:
        font_size = max(12, round(width * 0.057))
        font = ImageFont.load_default(size=font_size)
        padding = max(4, round(width * 0.019))
        line_height = ceil(font_size * 1.15)
    for index, (label, image) in enumerate(tiles):
        x, y = index % columns * width, index // columns * height
        if caption_overlay:
            grid.paste(image, (x + (width - image.width) // 2, y))
            identity, _, name = label.partition('\n')
            lines = [identity] + _caption_lines(draw, name, font, width - 2 * padding)
            band_height = len(lines) * line_height + 2 * padding
            top = y + height - band_height
            draw.rectangle((x, top, x + width - 1, y + height - 1), fill='#101820')
            for line_number, line in enumerate(lines):
                draw.text((x + padding, top + padding + line_number * line_height), line,
                          fill='white', font=font, anchor='lt')
        else:
            grid.paste(image, (x + (width - image.width) // 2, y + CAPTION_HEIGHT))
            draw.text((x + 6, y + 5), label, fill='white')
    output = BytesIO()
    grid.save(output, format='PNG', optimize=True)
    return output.getvalue()
