"""Build a real multi-size Windows .ico (BMP entries) for PyInstaller / Explorer."""

from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC_CANDIDATES = [
    Path(
        r"C:\Users\ThinkPad\.cursor\projects\e-Code-Repository-XcpOnPcan"
        r"\assets\cankit-icon-v2-a-can-wordmark.jpg"
    ),
    ROOT / "src" / "xcp_pcan" / "resources" / "cankit.png",
]
OUT_DIR = ROOT / "src" / "xcp_pcan" / "resources"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def _png_chunk(img: Image.Image) -> bytes:
    """PNG-compressed icon image (Windows Vista+; used for 256)."""
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _bmp_xor_and(img: Image.Image) -> bytes:
    """Classic 32bpp BGRA XOR bitmap + 1bpp AND mask (bottom-up)."""
    w, h = img.size
    rgba = img.convert("RGBA")
    pixels = list(rgba.getdata())
    # XOR bitmap: BGRA, bottom-up, rows padded to 4 bytes (already for 32bpp)
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        row = pixels[y * w : (y + 1) * w]
        for r, g, b, a in row:
            xor += bytes((b, g, r, a))
    # AND mask: 1 bit/pixel, padded to 32-bit rows, bottom-up
    row_bytes = ((w + 31) // 32) * 4
    and_mask = bytearray()
    for y in range(h - 1, -1, -1):
        row = pixels[y * w : (y + 1) * w]
        bits = 0
        count = 0
        out = bytearray(row_bytes)
        bi = 0
        for _r, _g, _b, a in row:
            bits = (bits << 1) | (1 if a < 128 else 0)
            count += 1
            if count == 8:
                out[bi] = bits
                bi += 1
                bits = 0
                count = 0
        if count:
            out[bi] = bits << (8 - count)
        and_mask += out
    # BITMAPINFOHEADER for icon: height is 2*h (XOR+AND)
    header = struct.pack(
        "<IIIHHIIIIII",
        40,  # biSize
        w,
        h * 2,
        1,  # planes
        32,  # bit count
        0,  # BI_RGB
        len(xor),
        0,
        0,
        0,
        0,
    )
    return header + bytes(xor) + bytes(and_mask)


def write_ico(path: Path, images: list[Image.Image]) -> None:
    entries: list[tuple[int, int, bytes]] = []
    for im in images:
        w, h = im.size
        if max(w, h) >= 256:
            data = _png_chunk(im)
        else:
            data = _bmp_xor_and(im)
        entries.append((w % 256, h % 256, data))  # 0 means 256 in ICO header

    count = len(entries)
    offset = 6 + 16 * count
    buf = bytearray()
    buf += struct.pack("<HHH", 0, 1, count)
    blobs: list[bytes] = []
    for w, h, data in entries:
        buf += struct.pack(
            "<BBBBHHII",
            w,
            h,
            0,  # color count
            0,  # reserved
            1,  # planes
            32,  # bit count
            len(data),
            offset,
        )
        blobs.append(data)
        offset += len(data)
    for blob in blobs:
        buf += blob
    path.write_bytes(buf)


def main() -> None:
    src = next(p for p in SRC_CANDIDATES if p.is_file())
    base = Image.open(src).convert("RGBA")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    png_path = OUT_DIR / "cankit.png"
    master = base.resize((256, 256), Image.Resampling.LANCZOS)
    master.save(png_path, "PNG")

    images: list[Image.Image] = []
    for s in SIZES:
        im = base.resize((s, s), Image.Resampling.LANCZOS)
        if s <= 32:
            bg = Image.new("RGBA", (s, s), (15, 23, 42, 255))
            bg.alpha_composite(im)
            im = bg
        images.append(im)

    ico_path = OUT_DIR / "cankit.ico"
    write_ico(ico_path, images)
    print(f"source={src}")
    print(f"wrote {png_path} ({png_path.stat().st_size} bytes)")
    print(f"wrote {ico_path} ({ico_path.stat().st_size} bytes)")
    hdr = ico_path.read_bytes()[:6]
    print(f"ico header={hdr.hex()} count={int.from_bytes(hdr[4:6], 'little')}")


if __name__ == "__main__":
    main()
