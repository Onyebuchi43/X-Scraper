"""
image_editor.py — Pixel-accurate dark mode Tweet Card renderer using Pillow.
Renders an exact replica of X/Twitter's native dark tweet UI:
  - Avatar image (uploaded file or initials)
  - Display Name + Verified Badge + X affiliate icon + @username
  - Top right X.com logo
  - Main tweet text body
  - Timestamp + Views count
  - Bottom action bar (Reply, Retweet, Like, Bookmark, Share icons + metrics)
"""
from __future__ import annotations

import io
import os
import math
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# Canvas dimensions (matching high-res tweet screenshot aspect ratio)
CARD_W = 1000

# Color palette (Exact X/Twitter Dark Mode)
BG_BLACK        = (0, 0, 0, 255)
NAME_WHITE      = (247, 249, 249, 255)
HANDLE_GREY     = (113, 118, 123, 255)
BODY_WHITE      = (231, 233, 234, 255)
BLUE_CHECK      = (29, 155, 240, 255)
META_GREY       = (113, 118, 123, 255)
ICON_GREY       = (113, 118, 123, 255)
BORDER_DARK     = (38, 42, 45, 255)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load best available font across Linux (Render/VPS), Windows, and bundled assets."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if bold:
        candidates = [
            os.path.join(base_dir, "assets", "fonts", "segoeuib.ttf"),
            os.path.join(base_dir, "assets", "fonts", "arialbd.ttf"),
            os.path.join(base_dir, "..", "assets", "fonts", "segoeuib.ttf"),
            os.path.join(base_dir, "..", "assets", "fonts", "arialbd.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
    else:
        candidates = [
            os.path.join(base_dir, "assets", "fonts", "segoeui.ttf"),
            os.path.join(base_dir, "assets", "fonts", "arial.ttf"),
            os.path.join(base_dir, "..", "assets", "fonts", "segoeui.ttf"),
            os.path.join(base_dir, "..", "assets", "fonts", "arial.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _load_symbol_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load Segoe UI Symbol for special glyphs (checkmark, arrows, etc.)."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for path in [
        os.path.join(base_dir, "assets", "fonts", "segoeui.ttf"),
        os.path.join(base_dir, "..", "assets", "fonts", "segoeui.ttf"),
        "C:/Windows/Fonts/seguisym.ttf",
        "C:/Windows/Fonts/symbol.ttf",
    ]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return _load_font(size)


# ── Geometry helpers ───────────────────────────────────────────────────────────
def _crop_circle(img: Image.Image, size: int) -> Image.Image:
    """Resize image to size×size and crop into a smooth circle."""
    img = img.resize((size, size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask=mask)
    return result


# ── Vector icon drawing (no font/emoji dependency) ────────────────────────────

def _draw_verified_badge(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 26) -> None:
    """Draw Twitter blue verified badge with a crisp vector checkmark."""
    draw.ellipse((x, y, x + size, y + size), fill=BLUE_CHECK)
    # Draw checkmark as polyline — always renders correctly regardless of font
    pad = size * 0.22
    cx, cy = x + size / 2, y + size / 2
    # Check points relative to center: left bottom, mid bottom, right top
    p1 = (x + pad, cy + size * 0.04)
    p2 = (cx - size * 0.05, cy + size * 0.28)
    p3 = (x + size - pad * 0.6, cy - size * 0.20)
    line_w = max(2, size // 9)
    draw.line([p1, p2, p3], fill=(255, 255, 255, 255), width=line_w)


def _draw_affiliate_x(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 22) -> None:
    """Draw the dark boxed X affiliate icon using vector lines."""
    draw.rounded_rectangle(
        (x, y, x + size, y + size), radius=4,
        fill=(15, 20, 25, 255), outline=(55, 60, 65, 255), width=1
    )
    pad = size * 0.28
    lw = max(2, size // 9)
    col = (210, 212, 214, 255)
    # Draw X: two diagonal lines
    draw.line([(x + pad, y + pad), (x + size - pad, y + size - pad)], fill=col, width=lw)
    draw.line([(x + size - pad, y + pad), (x + pad, y + size - pad)], fill=col, width=lw)


def _draw_x_logo_text(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 32) -> None:
    """Draw the top-right X.com text logo — large bold X then .com."""
    # Draw vector X instead of relying on 𝕏 Unicode glyph rendering
    lw = max(3, size // 7)
    col = NAME_WHITE
    x0, y0 = x, y
    # X shape — two crossing diagonals
    draw.line([(x0, y0), (x0 + size, y0 + size)], fill=col, width=lw)
    draw.line([(x0 + size, y0), (x0, y0 + size)], fill=col, width=lw)


def _draw_reply_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 20) -> None:
    """Draw a speech-bubble reply icon as vector."""
    lw = 2
    # Rounded rect as bubble
    draw.rounded_rectangle((x, y, x + size, y + size * 0.82), radius=size // 4,
                            outline=ICON_GREY, width=lw)
    # Triangle pointer bottom-left
    pts = [(x + size * 0.18, y + size * 0.82), (x + size * 0.08, y + size),
           (x + size * 0.38, y + size * 0.82)]
    draw.polygon(pts, fill=BG_BLACK)
    draw.line([(x + size * 0.18, y + size * 0.82), (x + size * 0.08, y + size)], fill=ICON_GREY, width=lw)
    draw.line([(x + size * 0.08, y + size), (x + size * 0.38, y + size * 0.82)], fill=ICON_GREY, width=lw)


def _draw_retweet_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 20) -> None:
    """Draw a retweet (two arrows in a rectangle loop) icon."""
    lw = 2
    r = size * 0.28
    # Top arrow →
    draw.line([(x + size * 0.1, y + size * 0.35), (x + size * 0.85, y + size * 0.35)], fill=ICON_GREY, width=lw)
    draw.line([(x + size * 0.7, y + size * 0.15), (x + size * 0.85, y + size * 0.35),
               (x + size * 0.7, y + size * 0.55)], fill=ICON_GREY, width=lw)
    # Bottom arrow ←
    draw.line([(x + size * 0.9, y + size * 0.65), (x + size * 0.15, y + size * 0.65)], fill=ICON_GREY, width=lw)
    draw.line([(x + size * 0.3, y + size * 0.45), (x + size * 0.15, y + size * 0.65),
               (x + size * 0.3, y + size * 0.85)], fill=ICON_GREY, width=lw)


def _draw_heart_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 20) -> None:
    """Draw a heart icon using two semi-circles + a V polygon."""
    lw = 2
    # Two circles for top bumps
    r = size * 0.25
    draw.arc((x + size * 0.05, y + size * 0.1,
              x + size * 0.55, y + size * 0.6), start=180, end=0, fill=ICON_GREY, width=lw)
    draw.arc((x + size * 0.45, y + size * 0.1,
              x + size * 0.95, y + size * 0.6), start=180, end=0, fill=ICON_GREY, width=lw)
    # V bottom
    draw.line([(x + size * 0.05, y + size * 0.38), (x + size * 0.5, y + size * 0.9)],
              fill=ICON_GREY, width=lw)
    draw.line([(x + size * 0.95, y + size * 0.38), (x + size * 0.5, y + size * 0.9)],
              fill=ICON_GREY, width=lw)


def _draw_bookmark_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 20) -> None:
    """Draw a bookmark icon (rectangle with V notch at bottom)."""
    lw = 2
    pts = [
        (x + size * 0.2, y + size * 0.05),
        (x + size * 0.8, y + size * 0.05),
        (x + size * 0.8, y + size * 0.95),
        (x + size * 0.5, y + size * 0.70),
        (x + size * 0.2, y + size * 0.95),
    ]
    draw.polygon(pts, outline=ICON_GREY, fill=BG_BLACK, width=lw)


def _draw_share_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 20) -> None:
    """Draw an upload/share arrow icon."""
    lw = 2
    cx = x + size * 0.5
    # Vertical stem
    draw.line([(cx, y + size * 0.4), (cx, y + size * 0.95)], fill=ICON_GREY, width=lw)
    # Arrow head pointing up
    draw.line([(cx, y + size * 0.05), (cx - size * 0.28, y + size * 0.38)], fill=ICON_GREY, width=lw)
    draw.line([(cx, y + size * 0.05), (cx + size * 0.28, y + size * 0.38)], fill=ICON_GREY, width=lw)
    # Base tray
    draw.line([(x + size * 0.1, y + size * 0.95), (x + size * 0.9, y + size * 0.95)],
              fill=ICON_GREY, width=lw)


import urllib.request


def _fetch_avatar_bytes(username: str) -> Optional[bytes]:
    """Attempt to fetch X profile picture via unavatar.io service with timeout."""
    clean_user = username.strip().lstrip("@")
    if not clean_user:
        return None
    url = f"https://unavatar.io/twitter/{clean_user}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            if resp.status == 200:
                data = resp.read()
                if len(data) > 500:
                    return data
    except Exception:
        pass
    return None


# ── Main renderer ──────────────────────────────────────────────────────────────
def generate_tweet_card_screenshot(
    name: str,
    username: str,
    body_text: str,
    avatar_bytes: Optional[bytes] = None,
    timestamp: str = "3:51 PM · 8/4/26",
    views: str = "3M",
    replies: str = "2.8K",
    retweets: str = "4.2K",
    likes: str = "54K",
    bookmarks: str = "1.4K",
) -> bytes:
    """
    Renders an exact dark-mode tweet card matching the screenshot template.

    Returns PNG image bytes.
    """
    PAD_X = 40
    PAD_Y = 36
    AVATAR_SIZE = 72

    # Auto-fetch avatar if not provided
    if not avatar_bytes and username:
        avatar_bytes = _fetch_avatar_bytes(username)

    # Fonts
    font_name      = _load_font(28, bold=True)
    font_handle    = _load_font(24, bold=False)
    font_body      = _load_font(30, bold=False)
    font_meta      = _load_font(22, bold=False)
    font_meta_bold = _load_font(22, bold=True)
    font_xcom      = _load_font(32, bold=True)   # for ".com" part of X.com
    font_icons     = _load_font(18, bold=False)

    # Wrap body text to width
    max_body_w = CARD_W - (PAD_X * 2)
    lines: list[str] = []
    dummy = Image.new("RGBA", (100, 100))
    d_dummy = ImageDraw.Draw(dummy)

    for paragraph in body_text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split(" ")
        cur_line = ""
        for word in words:
            test_line = f"{cur_line} {word}".strip()
            bbox = d_dummy.textbbox((0, 0), test_line, font=font_body)
            if bbox[2] - bbox[0] <= max_body_w:
                cur_line = test_line
            else:
                if cur_line:
                    lines.append(cur_line)
                cur_line = word
        if cur_line:
            lines.append(cur_line)

    line_height = 44
    body_h = max(len(lines), 1) * line_height

    # Total canvas height
    total_h = PAD_Y + AVATAR_SIZE + 30 + body_h + 36 + 30 + 28 + 52 + PAD_Y

    img = Image.new("RGBA", (CARD_W, total_h), BG_BLACK)
    draw = ImageDraw.Draw(img)

    # ── 1. Avatar ──────────────────────────────────────────────────────────────
    ava_x, ava_y = PAD_X, PAD_Y
    avatar_drawn = False
    if avatar_bytes:
        try:
            raw_ava = Image.open(io.BytesIO(avatar_bytes))
            circle_ava = _crop_circle(raw_ava, AVATAR_SIZE)
            img.paste(circle_ava, (ava_x, ava_y), circle_ava)
            avatar_drawn = True
        except Exception:
            pass

    if not avatar_drawn:
        ava_img = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), (45, 50, 55, 255))
        circle_ava = _crop_circle(ava_img, AVATAR_SIZE)
        img.paste(circle_ava, (ava_x, ava_y), circle_ava)
        draw.text(
            (ava_x + AVATAR_SIZE // 2, ava_y + AVATAR_SIZE // 2),
            (name[:1] or "?").upper(),
            fill=(240, 240, 240, 255),
            font=_load_font(36, bold=True),
            anchor="mm",
        )

    # ── 2. Header row: Name + badges + handle ─────────────────────────────────
    text_x = ava_x + AVATAR_SIZE + 18
    name_y = ava_y + 4
    draw.text((text_x, name_y), name, fill=NAME_WHITE, font=font_name)
    name_bbox = draw.textbbox((text_x, name_y), name, font=font_name)
    curr_x = name_bbox[2] + 10

    # Blue verified checkmark badge
    badge_size = 26
    _draw_verified_badge(draw, curr_x, name_y + 3, size=badge_size)

    # @username on second line
    handle_y = name_y + 36
    draw.text((text_x, handle_y), f"@{username.lstrip('@')}", fill=HANDLE_GREY, font=font_handle)

    # ── 4. Body text ──────────────────────────────────────────────────────────
    body_y = ava_y + AVATAR_SIZE + 28
    space_w = draw.textbbox((0, 0), " ", font=font_body)[2] - draw.textbbox((0, 0), "", font=font_body)[2]
    for i, line in enumerate(lines):
        if line:
            curr_line_x = PAD_X
            words = line.split(" ")
            for word in words:
                if not word:
                    curr_line_x += space_w
                    continue
                # Highlight @mentions, #hashtags, and links in Twitter Blue
                is_mention = word.startswith("@") or word.startswith("#") or word.lower().startswith("http")
                fill_color = BLUE_CHECK if is_mention else BODY_WHITE
                draw.text((curr_line_x, body_y + (i * line_height)), word, fill=fill_color, font=font_body)
                word_w = draw.textbbox((0, 0), word, font=font_body)[2] - draw.textbbox((0, 0), "", font=font_body)[2]
                curr_line_x += word_w + space_w

    # ── 5. Divider line under body ────────────────────────────────────────────
    div_y = body_y + body_h + 18
    draw.line([(PAD_X, div_y), (CARD_W - PAD_X, div_y)], fill=BORDER_DARK, width=1)

    # ── 6. Timestamp & Views row ──────────────────────────────────────────────
    meta_y = div_y + 14
    meta_str = timestamp if timestamp else "3:51 PM · 8/4/26"
    draw.text((PAD_X, meta_y), meta_str, fill=META_GREY, font=font_meta)
    meta_bbox = draw.textbbox((PAD_X, meta_y), meta_str, font=font_meta)

    dot_x = meta_bbox[2] + 10
    draw.text((dot_x, meta_y), "·", fill=META_GREY, font=font_meta)
    dot_w_bbox = draw.textbbox((dot_x, meta_y), "·", font=font_meta)
    views_x = dot_w_bbox[2] + 10
    views_str = views if views else "3M"
    draw.text((views_x, meta_y), views_str, fill=NAME_WHITE, font=font_meta_bold)
    v_bbox = draw.textbbox((views_x, meta_y), views_str, font=font_meta_bold)
    draw.text((v_bbox[2] + 6, meta_y), "Views", fill=META_GREY, font=font_meta)

    # ── 7. Divider line under meta ────────────────────────────────────────────
    div2_y = meta_y + 30
    draw.line([(PAD_X, div2_y), (CARD_W - PAD_X, div2_y)], fill=BORDER_DARK, width=1)

    # ── 8. Action bar — vector icons ─────────────────────────────────────────
    action_y = div2_y + 14
    icon_size = 22
    col_w = (CARD_W - (PAD_X * 2)) // 5

    icon_fns = [
        _draw_reply_icon,
        _draw_retweet_icon,
        _draw_heart_icon,
        _draw_bookmark_icon,
        _draw_share_icon,
    ]
    counts = [replies or "2.8K", retweets or "4.2K", likes or "54K", bookmarks or "1.4K", ""]

    for idx, (icon_fn, count) in enumerate(zip(icon_fns, counts)):
        cx = PAD_X + (idx * col_w)
        icon_fn(draw, cx, action_y, size=icon_size)
        if count:
            draw.text((cx + icon_size + 7, action_y + 2), count, fill=ICON_GREY, font=font_meta)

    # Return PNG bytes
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


# Alias for backward compatibility
def generate_tweet_card(*args, **kwargs) -> bytes:
    return generate_tweet_card_screenshot(*args, **kwargs)
