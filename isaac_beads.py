#!/usr/bin/env python3
"""Build numbered perler bead charts from Binding of Isaac Wiki icons.

The script uses the public MediaWiki API to discover image files on a small,
curated set of wiki pages. It then converts each icon into a square bead grid,
quantizes the visible colours to the full 221-colour MARD palette, and writes
one annotated PNG per source icon. Every occupied cell shows its bead colour
code directly, with a compact colour/count summary at the bottom.

Example:
    python isaac_beads.py build --categories core --size 29
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import posixpath
import re
import secrets
import threading
import time
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urljoin, urlsplit

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, ImageDraw, ImageFont


WIKI_ROOT = "https://bindingofisaacrebirth.wiki.gg"
WIKI_API = f"{WIKI_ROOT}/api.php"
USER_AGENT = (
    "PlayBeansIsaacPersonalTool/1.0 "
    "(personal, non-commercial sprite-to-perler project)"
)
PALETTE_KEY = "mard"


@dataclass(frozen=True)
class WikiCategory:
    key: str
    label: str
    page: str
    prefixes: tuple[str, ...]


CATEGORIES: dict[str, WikiCategory] = {
    "collectibles": WikiCategory(
        "collectibles", "道具", "Items", ("Collectible_",)
    ),
    "characters": WikiCategory(
        "characters", "人物", "Characters", ("Character_",)
    ),
    "trinkets": WikiCategory(
        "trinkets", "饰品", "Trinkets", ("Trinket_",)
    ),
    "cards": WikiCategory(
        "cards",
        "卡牌与符文",
        "Cards and Runes",
        ("Pickup_", "Card_", "Rune_", "Soul_", "Blank_"),
    ),
    "bosses": WikiCategory("bosses", "Boss", "Bosses", ("Boss_",)),
    "pills": WikiCategory("pills", "药丸", "Pills", ("Pill_",)),
}

# Groups are expanded at runtime. "core" is deliberately broad but excludes
# pages dominated by non-icon decorations.
CATEGORY_GROUPS = {
    "core": ("collectibles", "characters", "trinkets", "cards", "bosses"),
    "all": tuple(CATEGORIES),
}


PALETTE_FILE = Path(__file__).with_name("palettes") / "mard221.json"
PALETTE: dict[str, str] = json.loads(PALETTE_FILE.read_text(encoding="utf-8"))
if len(PALETTE) != 221:
    raise RuntimeError(f"Expected 221 MARD colours, found {len(PALETTE)}")


class _ImageTagParser(HTMLParser):
    """Collect image URLs and alt text from MediaWiki-rendered HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        src = values.get("src") or values.get("data-src")
        if not src or src.startswith("data:"):
            return
        self.images.append((src, values.get("alt", "").strip()))


class WikiClient:
    def __init__(self, cache_dir: Path, refresh: bool = False) -> None:
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _request(self, url: str, **kwargs: object) -> requests.Response:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json,image/*,*/*"}
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = requests.get(
                    url, headers=headers, timeout=45, **kwargs  # type: ignore[arg-type]
                )
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                response.raise_for_status()
                return response
            except Exception as exc:  # requests raises several concrete classes
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(0.6 * (2**attempt))
        assert last_error is not None
        raise last_error

    def page_images(self, category: WikiCategory) -> list[dict[str, str]]:
        cache_file = self.cache_dir / f"{category.key}.json"
        if cache_file.exists() and not self.refresh:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            params = {
                "action": "parse",
                "page": category.page,
                "prop": "text",
                "format": "json",
                "formatversion": "2",
                "redirects": "1",
                "disablelimitreport": "1",
            }
            response = self._request(WIKI_API, params=params)
            payload = response.json()
            if "error" in payload:
                raise RuntimeError(
                    f"Wiki API error for {category.page}: {payload['error'].get('info')}"
                )
            cache_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

        html = payload["parse"]["text"]
        parser = _ImageTagParser()
        parser.feed(html)

        seen: set[str] = set()
        result: list[dict[str, str]] = []
        for src, alt in parser.images:
            absolute = urljoin(WIKI_ROOT + "/", src)
            path_name = unquote(posixpath.basename(urlsplit(absolute).path))
            path_name = re.sub(r"^\d+px-", "", path_name)
            if not path_name.lower().endswith((".png", ".webp", ".gif")):
                continue
            if not any(path_name.startswith(prefix) for prefix in category.prefixes):
                continue
            if path_name in seen:
                continue
            seen.add(path_name)
            stem = Path(path_name).stem
            fallback = friendly_name(stem, category)
            result.append(
                {
                    "category": category.key,
                    "category_label": category.label,
                    "source_page": category.page,
                    "source_page_url": f"{WIKI_ROOT}/wiki/{category.page.replace(' ', '_')}",
                    "source_image": path_name,
                    "source_url": absolute,
                    "title": alt or fallback,
                    "fallback_title": fallback,
                }
            )
        result.sort(key=lambda row: row["title"].casefold())
        assign_entry_slugs(result)
        return result

    def download_image(self, url: str, destination: Path) -> None:
        if destination.exists() and destination.stat().st_size > 0:
            return
        response = self._request(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(response.content)
        temporary.replace(destination)


_THREAD_LOCAL = threading.local()


def _session_for_thread() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        _THREAD_LOCAL.session = session
    return session


def _download_one(entry: dict[str, str], destination: Path) -> str:
    if destination.exists() and destination.stat().st_size > 0:
        return "cached"
    session = _session_for_thread()
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = session.get(entry["source_url"], timeout=45)
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            response.raise_for_status()
            if not response.content:
                raise RuntimeError("empty image response")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".part")
            temporary.write_bytes(response.content)
            temporary.replace(destination)
            time.sleep(0.04)
            return "downloaded"
        except Exception as exc:
            last_error = exc
            if attempt == 3:
                break
            time.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"{entry['source_url']}: {last_error}")


def friendly_name(stem: str, category: WikiCategory) -> str:
    value = stem
    for prefix in category.prefixes:
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    value = re.sub(r"_(?:icon|Icon)$", "", value)
    value = value.replace("_", " ").strip()
    return value or stem


def safe_slug(value: str, suffix: str = "") -> str:
    value = unquote(value)
    value = value.replace("'", "").replace("’", "")
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    if not value:
        value = "image_" + hashlib.sha1(suffix.encode("utf-8")).hexdigest()[:8]
    return value[:150]


def assign_entry_slugs(entries: list[dict[str, str]]) -> None:
    used: dict[str, int] = {}
    for entry in entries:
        base = safe_slug(
            Path(entry["source_image"]).stem,
            entry["source_image"],
        )
        if base in used:
            used[base] += 1
            entry["slug"] = f"{base}_{used[base]}"
        else:
            used[base] = 1
            entry["slug"] = base


def parse_hex(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    def linear(channel: int) -> float:
        value = channel / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(channel) for channel in rgb)
    x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / 0.95047
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = (r * 0.0193339 + g * 0.1191920 + b * 0.9503041) / 1.08883

    def f(value: float) -> float:
        delta = 6 / 29
        return value ** (1 / 3) if value > delta**3 else value / (3 * delta**2) + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


PALETTE_RGB = {key: parse_hex(value) for key, value in PALETTE.items()}
PALETTE_LAB = {key: rgb_to_lab(value) for key, value in PALETTE_RGB.items()}
MATCHABLE_LAB = {key: value for key, value in PALETTE_LAB.items() if key != "H1"}
_colour_cache: dict[tuple[int, int, int], str] = {}


def nearest_palette_key(rgb: tuple[int, int, int]) -> str:
    cached = _colour_cache.get(rgb)
    if cached is not None:
        return cached
    lab = rgb_to_lab(rgb)
    key = min(
        MATCHABLE_LAB,
        key=lambda item: sum(
            (lab[index] - PALETTE_LAB[item][index]) ** 2 for index in range(3)
        ),
    )
    _colour_cache[rgb] = key
    return key


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for name in (
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "arialbd.ttf",
        "arial.ttf",
        "segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def prepare_rgba(
    image: Image.Image,
    size: int,
    resample: str,
    margin: int,
    alpha_threshold: int,
) -> Image.Image:
    source = image.convert("RGBA")
    alpha = source.getchannel("A").point(
        lambda value: 255 if value >= alpha_threshold else 0
    )
    source.putalpha(alpha)
    bbox = alpha.getbbox()
    if bbox:
        source = source.crop(bbox)
    if source.width < 1 or source.height < 1:
        raise ValueError("image has no visible pixels")

    inner = max(1, size - margin * 2)
    scale = min(inner / source.width, inner / source.height)
    width = max(1, min(inner, round(source.width * scale)))
    height = max(1, min(inner, round(source.height * scale)))
    method = Image.Resampling.NEAREST if resample == "nearest" else Image.Resampling.LANCZOS
    resized = source.resize((width, height), method)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    left = (size - width) // 2
    top = (size - height) // 2
    canvas.alpha_composite(resized, (left, top))
    return canvas


def quantize_image(
    prepared: Image.Image, alpha_threshold: int
) -> tuple[dict[tuple[int, int], str], dict[str, int]]:
    beads: dict[tuple[int, int], str] = {}
    counts: dict[str, int] = {}
    pixels = prepared.load()
    for y in range(prepared.height):
        for x in range(prepared.width):
            red, green, blue, alpha = pixels[x, y]
            if alpha < alpha_threshold:
                continue
            key = nearest_palette_key((red, green, blue))
            beads[(x, y)] = key
            counts[key] = counts.get(key, 0) + 1
    return beads, counts


def _palette_sort_key(key: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", key)
    if not match:
        return key, 0
    return match.group(1), int(match.group(2))


def _text_colour(hex_colour: str) -> str:
    red, green, blue = parse_hex(hex_colour)
    luminance = (red * 299 + green * 587 + blue * 114) / 1000
    return "#111111" if luminance >= 145 else "#FFFFFF"


def render_annotated_chart(
    title: str,
    beads: dict[tuple[int, int], str],
    counts: dict[str, int],
    size: int,
    cell: int = 42,
) -> Image.Image:
    """Render one self-contained PNG with a bead code inside every cell."""

    margin = 28
    row_label_width = 42
    header_height = 88
    column_label_height = 30
    grid_width = size * cell
    grid_height = size * cell
    grid_left = margin + row_label_width
    grid_top = header_height + column_label_height

    legend_item_width = 190
    legend_columns = max(3, min(6, grid_width // legend_item_width))
    legend_keys = sorted(counts, key=_palette_sort_key)
    legend_rows = max(1, math.ceil(len(legend_keys) / legend_columns))
    legend_height = 46 + legend_rows * 30
    footer_height = 42
    canvas_width = margin * 2 + row_label_width + grid_width
    canvas_height = grid_top + grid_height + legend_height + footer_height

    canvas = Image.new("RGB", (canvas_width, canvas_height), "#F5F3EE")
    draw = ImageDraw.Draw(canvas)
    title_font = _font(26)
    subtitle_font = _font(16)
    small_font = _font(11)
    code_font = _font(13)
    legend_font = _font(14)

    draw.text((margin, 20), title, font=title_font, fill="#171A1F")
    subtitle = (
        f"{size} × {size} 钉 | 221色-MARD,DoDo,小舞家、黄豆豆 | "
        f"共 {sum(counts.values())} 颗豆 | {len(counts)} 种颜色"
    )
    draw.text((margin, 56), subtitle, font=subtitle_font, fill="#525A64")

    for y in range(size):
        top = grid_top + y * cell
        for x in range(size):
            left = grid_left + x * cell
            key = beads.get((x, y))
            if key is None:
                draw.rectangle(
                    (left, top, left + cell - 1, top + cell - 1),
                    fill="#E9EDF2",
                )
                continue
            colour = PALETTE[key]
            draw.rectangle(
                (left, top, left + cell - 1, top + cell - 1),
                fill=colour,
            )
            draw.text(
                (left + cell / 2, top + cell / 2 - 1),
                key,
                font=code_font,
                fill=_text_colour(colour),
                anchor="mm",
            )

    # Column and row coordinates make it easy to follow large charts on a pegboard.
    for index in range(size):
        x = grid_left + index * cell + cell / 2
        y = grid_top + index * cell + cell / 2
        draw.text(
            (x, grid_top - column_label_height / 2 - 1),
            str(index + 1),
            font=small_font,
            fill="#30363D",
            anchor="mm",
        )
        draw.text(
            (grid_left - row_label_width / 2, y),
            str(index + 1),
            font=small_font,
            fill="#30363D",
            anchor="mm",
        )

    # Light guides and stronger five-cell guides.
    for index in range(size + 1):
        x = grid_left + index * cell
        y = grid_top + index * cell
        width = 3 if index % 5 == 0 or index in (0, size) else 1
        colour = "#66717E" if width == 3 else "#AFB7C0"
        draw.line((x, grid_top, x, grid_top + grid_height), fill=colour, width=width)
        draw.line((grid_left, y, grid_left + grid_width, y), fill=colour, width=width)

    legend_top = grid_top + grid_height + 14
    draw.text(
        (margin, legend_top),
        f"色号 / 数量（{sum(counts.values())} 颗）",
        font=_font(18),
        fill="#171A1F",
    )
    legend_start_y = legend_top + 32
    usable_width = canvas_width - margin * 2
    actual_item_width = usable_width / legend_columns
    for index, key in enumerate(legend_keys):
        column = index % legend_columns
        line = index // legend_columns
        left = margin + column * actual_item_width
        top = legend_start_y + line * 30
        swatch_size = 18
        draw.rounded_rectangle(
            (left, top, left + swatch_size, top + swatch_size),
            radius=3,
            fill=PALETTE[key],
            outline="#6D7680",
            width=1,
        )
        draw.text(
            (left + swatch_size + 7, top + swatch_size / 2),
            f"{key} × {counts[key]}",
            font=legend_font,
            fill="#252A31",
            anchor="lm",
        )

    footer_y = canvas_height - footer_height + 8
    draw.text(
        (margin, footer_y),
        "空白格 = 不放豆 | 图片来源：bindingofisaacrebirth.wiki.gg | 仅供个人拼豆制作",
        font=small_font,
        fill="#69727C",
    )
    return canvas


def _pkcs7_pad(data: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    return padder.update(data) + padder.finalize()


def _evp_bytes_to_key(password: bytes, salt: bytes) -> tuple[bytes, bytes]:
    derived = b""
    block = b""
    while len(derived) < 48:
        block = hashlib.md5(block + password + salt).digest()
        derived += block
    return derived[:32], derived[32:48]


def _mask_passphrase(passphrase: str) -> tuple[str, str]:
    substitutions = {
        "a": "z",
        "s": "x",
        "d": "c",
        "f": "v",
        "g": "b",
        "h": "n",
        "j": "m",
        "k": ",",
        "l": ".",
    }
    masked = ""
    positions = ""
    for index, char in enumerate(passphrase):
        if char in substitutions:
            masked += substitutions[char]
            positions += str(index)
        else:
            masked += char
    if not positions:
        masked = passphrase[::-1]
    return masked, positions


def encrypt_playbeans_json(payload: dict[str, object]) -> str:
    """Return the CryptoJS-compatible text format used by PlayBeans .beans."""

    plaintext = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    passphrase = "".join(secrets.choice(alphabet) for _ in range(10))
    salt = os.urandom(8)
    key, iv = _evp_bytes_to_key(passphrase.encode("ascii"), salt)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()
    import base64

    encoded = base64.b64encode(b"Salted__" + salt + ciphertext).decode("ascii")
    masked, positions = _mask_passphrase(passphrase)
    return f"{masked}/{encoded}/{positions}"


def decrypt_playbeans_text(text: str) -> dict[str, object]:
    """Small independent round-trip decoder used by self-tests."""

    first = text.find("/")
    last = text.rfind("/")
    if first < 0 or last <= first:
        raise ValueError("invalid .beans envelope")
    masked = text[:first]
    encoded = text[first + 1 : last]
    positions = text[last + 1 :]
    reverse = {
        "z": "a",
        "x": "s",
        "c": "d",
        "v": "f",
        "b": "g",
        "n": "h",
        "m": "j",
        ",": "k",
        ".": "l",
    }
    chars = list(masked)
    if positions:
        for position in positions:
            index = int(position)
            if index < len(chars) and chars[index] in reverse:
                chars[index] = reverse[chars[index]]
        passphrase = "".join(chars)
    else:
        passphrase = masked[::-1]

    import base64

    raw = base64.b64decode(encoded)
    if not raw.startswith(b"Salted__"):
        raise ValueError("missing OpenSSL salt header")
    salt = raw[8:16]
    key, iv = _evp_bytes_to_key(passphrase.encode("ascii"), salt)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return json.loads(plaintext.decode("utf-8"))


def make_beans_payload(
    beads: dict[tuple[int, int], str], size: int
) -> dict[str, object]:
    by_colour: dict[str, list[str]] = {}
    for (x, y), key in sorted(beads.items(), key=lambda item: (item[0][1], item[0][0])):
        by_colour.setdefault(key, []).append(f"{x},{y}")
    return {
        "beans": by_colour,
        "GRID_WIDTH": size,
        "GRID_HEIGHT": size,
        "colorSwatchKey": PALETTE_KEY,
        "beanType": "bean",
    }


def write_beans(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encrypt_playbeans_json(payload), encoding="utf-8")


def process_category(
    category: WikiCategory,
    entries: list[dict[str, str]],
    output: Path,
    size: int,
    resample: str,
    margin: int,
    alpha_threshold: int,
    write_beans_files: bool,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    total = len(entries)
    for number, entry in enumerate(entries, start=1):
        slug = entry.get("slug") or safe_slug(
            Path(entry["source_image"]).stem,
            entry["source_image"],
        )

        original = output / "originals" / category.key / f"{slug}.png"
        annotated_path = output / "png" / category.key / f"{slug}.png"
        beans_path = output / "beans" / category.key / f"{slug}.beans"
        try:
            with Image.open(original) as opened:
                prepared = prepare_rgba(
                    opened,
                    size=size,
                    resample=resample,
                    margin=margin,
                    alpha_threshold=alpha_threshold,
                )
            beads, counts = quantize_image(prepared, alpha_threshold)
            if not beads:
                raise ValueError("no visible pixels after alpha threshold")
            title = entry.get("title") or entry.get("fallback_title") or slug
            annotated = render_annotated_chart(title, beads, counts, size)
            annotated_path.parent.mkdir(parents=True, exist_ok=True)
            annotated.save(annotated_path, optimize=True)
            if write_beans_files:
                payload = make_beans_payload(beads, size)
                write_beans(beans_path, payload)
            rows.append(
                {
                    **entry,
                    "slug": slug,
                    "title": title,
                    "original_file": str(original.relative_to(output)).replace("\\", "/"),
                    "png_file": str(annotated_path.relative_to(output)).replace("\\", "/"),
                    "beans_file": (
                        str(beans_path.relative_to(output)).replace("\\", "/")
                        if write_beans_files
                        else ""
                    ),
                    "bead_count": len(beads),
                    "color_count": len(counts),
                    "colors": ",".join(
                        f"{key}:{count}"
                        for key, count in sorted(
                            counts.items(),
                            key=lambda item: (_palette_sort_key(item[0]), item[1]),
                        )
                    ),
                    "status": "ok",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    **entry,
                    "slug": slug,
                    "bead_count": 0,
                    "color_count": 0,
                    "colors": "",
                    "status": "error",
                    "error": str(exc),
                }
            )
        if number % 25 == 0 or number == total:
            print(
                f"[{category.key}] processed {number}/{total}",
                flush=True,
            )
    return rows


def download_category(
    client: WikiClient,
    category: WikiCategory,
    entries: list[dict[str, str]],
    output: Path,
    workers: int,
) -> list[dict[str, object]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    prepared: list[dict[str, object]] = []
    total = len(entries)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {}
        for entry in entries:
            slug = entry.get("slug") or safe_slug(
                Path(entry["source_image"]).stem,
                entry["source_image"],
            )
            destination = output / "originals" / category.key / f"{slug}.png"
            future = executor.submit(_download_one, entry, destination)
            futures[future] = (entry, destination)
        completed = 0
        for future in as_completed(futures):
            entry, destination = futures[future]
            completed += 1
            try:
                state = future.result()
                prepared.append(
                    {
                        **entry,
                        "download_status": state,
                        "original_path": str(destination),
                    }
                )
            except Exception as exc:
                prepared.append({**entry, "download_error": str(exc)})
            if completed % 25 == 0 or completed == total:
                print(
                    f"[{category.key}] downloaded {completed}/{total}",
                    flush=True,
                )
    return prepared


def write_records(rows: Sequence[dict[str, object]], output: Path) -> None:
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(list(rows), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fields = [
        "category",
        "category_label",
        "title",
        "source_page",
        "source_page_url",
        "source_image",
        "source_url",
        "original_file",
        "png_file",
        "beans_file",
        "bead_count",
        "color_count",
        "colors",
        "status",
        "error",
    ]
    csv_path = output / "inventory.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_readme(output: Path, size: int) -> None:
    text = f"""以撒拼豆图（色号标注 PNG）

生成尺寸：{size} × {size} 豆
色卡：221色-MARD,DoDo,小舞家、黄豆豆

每个 Wiki 图标对应 png/ 目录中的一张独立 PNG。图中每颗豆都直接标出色号，
顶部和左侧带行列坐标，底部列出全部色号及各自用量。

目录说明
- originals/：从 Isaac Wiki 下载的原始图标。
- png/：最终成品，每个图标一张带色号的 PNG。
- beans/：仅在使用 --beans 参数时生成的 PlayBeans 工程文件。
- inventory.csv：每个图标的颜色号、豆子数量和来源。
- manifest.json：完整来源与处理记录。

提示
- 透明像素表示不放豆。
- 小图标缩到 {size}×{size} 时不可避免会丢失少量细节；可用 --size 21 生成更省豆的版本。
- 图源来自 The Binding of Isaac: Rebirth Wiki。图片权利归原作者/权利方所有，
  请仅用于个人拼豆制作，不要将下载图包作为商业素材重新发布。
"""
    (output / "README.txt").write_text(text, encoding="utf-8")


def make_zip(output: Path, size: int) -> Path:
    zip_path = output.parent / f"isaac-beads-mard221-{size}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(output)
            if relative.parts[0] != "png" and path.name not in {
                "README.txt",
                "inventory.csv",
                "manifest.json",
                "summary.json",
            }:
                continue
            archive.write(path, path.relative_to(output.parent))
    return zip_path


def expand_categories(value: str) -> list[WikiCategory]:
    selected: list[str] = []
    for item in value.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item in CATEGORY_GROUPS:
            selected.extend(CATEGORY_GROUPS[item])
        elif item in CATEGORIES:
            selected.append(item)
        else:
            valid = ", ".join([*CATEGORIES, *CATEGORY_GROUPS])
            raise ValueError(f"Unknown category {item!r}. Choose from: {valid}")
    deduplicated = list(dict.fromkeys(selected))
    if not deduplicated:
        raise ValueError("No categories selected")
    return [CATEGORIES[key] for key in deduplicated]


def build(args: argparse.Namespace) -> int:
    categories = expand_categories(args.categories)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cache_dir = output / ".cache" / "wiki"
    client = WikiClient(cache_dir, refresh=args.refresh)
    all_rows: list[dict[str, object]] = []

    print(
        f"Building {len(categories)} categories at {args.size}x{args.size}, "
        f"MARD 221 palette, output={output}",
        flush=True,
    )
    for category in categories:
        print(f"Reading wiki page: {category.page}", flush=True)
        entries = client.page_images(category)
        if args.limit and args.limit > 0:
            entries = entries[: args.limit]
        print(f"[{category.key}] found {len(entries)} images", flush=True)
        downloaded = download_category(
            client, category, entries, output, workers=args.workers
        )
        downloadable = [
            row
            for row in downloaded
            if row.get("status") not in {"error"} and row.get("original_path")
        ]
        # Keep source metadata but pass the actual downloaded image path onward.
        prepared_entries: list[dict[str, str]] = []
        for row in downloadable:
            prepared_entries.append(
                {
                    **row,
                    "original_path": str(row["original_path"]),
                }
            )
        category_rows = process_category(
            category,
            prepared_entries,
            output,
            size=args.size,
            resample=args.resample,
            margin=args.margin,
            alpha_threshold=args.alpha_threshold,
            write_beans_files=args.beans,
        )
        for row in downloaded:
            if not row.get("original_path"):
                all_rows.append(
                    {
                        **row,
                        "status": "error",
                        "error": row.get("download_error", "download failed"),
                    }
                )
        all_rows.extend(category_rows)

    write_records(all_rows, output)
    write_readme(output, args.size)
    summary = {
        "generator": "isaac_beads.py",
        "wiki_api": WIKI_API,
        "size": args.size,
        "palette": PALETTE_KEY,
        "categories": [category.key for category in categories],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ok": sum(row.get("status") == "ok" for row in all_rows),
        "failed": sum(row.get("status") != "ok" for row in all_rows),
        "beads_total": sum(int(row.get("bead_count") or 0) for row in all_rows),
        "png_files": sum(row.get("status") == "ok" for row in all_rows),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not args.no_zip:
        zip_path = make_zip(output, args.size)
        summary["zip"] = str(zip_path)
        print(f"Wrote {zip_path}", flush=True)
    print(
        f"Done: {summary['ok']} patterns, {summary['failed']} failures, "
        f"{summary['beads_total']} beads",
        flush=True,
    )
    return 0 if summary["failed"] == 0 else 2


def process_existing(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    rows: list[dict[str, object]] = []
    for category in expand_categories(args.categories):
        directory = output / "originals" / category.key
        if not directory.exists():
            print(f"Skipping missing directory: {directory}", flush=True)
            continue
        entries: list[dict[str, str]] = []
        for path in sorted(directory.glob("*.png")):
            title = friendly_name(path.stem, category)
            entries.append(
                {
                    "category": category.key,
                    "category_label": category.label,
                    "source_page": "local",
                    "source_page_url": "",
                    "source_image": path.name,
                    "source_url": "",
                    "title": title,
                    "fallback_title": title,
                    "original_path": str(path),
                }
            )
        assign_entry_slugs(entries)
        rows.extend(
            process_category(
                category,
                entries,
                output,
                args.size,
                args.resample,
                args.margin,
                args.alpha_threshold,
                args.beans,
            )
        )
    write_records(rows, output)
    write_readme(output, args.size)
    if not args.no_zip:
        print(f"Wrote {make_zip(output, args.size)}", flush=True)
    return 0


def self_test() -> int:
    payload = {
        "beans": {"H7": ["1,2"], "H2": ["2,2"]},
        "GRID_WIDTH": 3,
        "GRID_HEIGHT": 3,
        "colorSwatchKey": PALETTE_KEY,
        "beanType": "bean",
    }
    encoded = encrypt_playbeans_json(payload)
    decoded = decrypt_playbeans_text(encoded)
    assert decoded == payload
    assert len(PALETTE) == 221
    print("Self-test passed: 221 colours and optional .beans encoding are valid.")
    return 0


def common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--categories",
        default="core",
        help="Comma-separated categories or groups: core, all, collectibles, "
        "characters, trinkets, cards, bosses, pills",
    )
    parser.add_argument("--size", type=int, default=29, help="Grid size; default 29")
    parser.add_argument(
        "--output", default="output/isaac-beads-221", help="Output folder"
    )
    parser.add_argument(
        "--resample",
        choices=("lanczos", "nearest"),
        default="lanczos",
        help="Image scaling method; nearest preserves hard pixel edges",
    )
    parser.add_argument("--margin", type=int, default=1, help="Empty border in beads")
    parser.add_argument(
        "--alpha-threshold",
        type=int,
        default=128,
        help="Pixels below this alpha become empty cells",
    )
    parser.add_argument(
        "--no-zip", action="store_true", help="Do not create the distributable zip"
    )
    parser.add_argument(
        "--beans",
        action="store_true",
        help="Also write optional PlayBeans .beans project files",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download Isaac Wiki sprite icons and create numbered bead PNGs."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Download and generate patterns")
    common_arguments(build_parser)
    build_parser.add_argument(
        "--workers", type=int, default=4, help="Concurrent image downloads; default 4"
    )
    build_parser.add_argument(
        "--limit", type=int, default=0, help="Process only the first N images per category"
    )
    build_parser.add_argument(
        "--refresh", action="store_true", help="Refresh cached wiki page data"
    )
    build_parser.set_defaults(handler=build)

    process_parser = subparsers.add_parser(
        "process", help="Rebuild patterns from already downloaded originals"
    )
    common_arguments(process_parser)
    process_parser.set_defaults(handler=process_existing)

    subparsers.add_parser("self-test", help="Validate palette and .beans encoding")
    args = parser.parse_args(argv)
    if args.command == "self-test":
        return self_test()
    if not 8 <= args.size <= 256:
        parser.error("--size must be between 8 and 256")
    if not 0 <= args.margin < args.size // 2:
        parser.error("--margin is outside the valid range")
    if not 1 <= args.alpha_threshold <= 255:
        parser.error("--alpha-threshold must be between 1 and 255")
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
