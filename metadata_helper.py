#!/usr/bin/env python3
"""
metadata_helper.py — Adobe Stock / Shutterstock contributor compliance.

Takes Recraft's native WebP and produces a stock-ready JPEG:

  * 100 % quality JPEG in sRGB with an EMBEDDED ICC profile
    (portals reject WebP and CMYK / untagged colour spaces).
  * True IPTC records (2:xx IIM in the Photoshop APP13 block, via
    iptcinfo3) so title / description / keywords auto-populate on upload:
        2:05   ObjectName        -> title
        2:120  Caption-Abstract  -> description
        2:25   Keywords          -> repeated array records (max 49)
        2:80   By-line, 2:116 Copyright notice (optional)
  * EXIF ImageDescription + Microsoft XP* tags for Windows Explorer.
  * XMP packet (dc:title / dc:description / dc:subject + Iptc4xmpCore)
    for portals/DAMs that read XMP.

Verified on this machine: Pillow 12 writes xmp= natively and iptcinfo3's
in-place rewrite preserves ICC + XMP + EXIF.
"""

from __future__ import annotations

import re
from pathlib import Path

import piexif
from PIL import Image, ImageCms
from iptcinfo3 import IPTCInfo

MAX_KEYWORDS = 49          # Adobe Stock caps at 49 (Shutterstock 50)
MAX_TITLE = 200
MAX_DESCRIPTION = 2000
DEFAULT_CREATOR = ""

# ---------------------------------------------------------------------------
# sRGB ICC profile (embedded so the JPEG is explicitly tagged)
# ---------------------------------------------------------------------------
_SRGB_ICC: bytes | None = None


def get_srgb_icc() -> bytes:
    global _SRGB_ICC
    if _SRGB_ICC is None:
        try:
            _SRGB_ICC = ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")).tobytes()
        except Exception:
            _SRGB_ICC = b""
    return _SRGB_ICC


# ---------------------------------------------------------------------------
# Text / keyword cleaning
# ---------------------------------------------------------------------------
_SPLIT_RE = re.compile(r"[;,\n|]+")
_JUNK_RE = re.compile(r"[^a-z0-9\s\-'/&().]+")


def clean_keywords(raw) -> list[str]:
    """CSV/semicolon string or list -> cleaned lowercase keyword array,
    deduped, capped at 49 (contributor-guideline limit)."""
    if raw is None:
        return []
    parts = [str(x) for x in raw] if isinstance(raw, (list, tuple)) \
        else _SPLIT_RE.split(str(raw))
    out, seen = [], set()
    for p in parts:
        k = re.sub(r"\s+", " ", _JUNK_RE.sub(" ", p.lower())).strip()
        if not k or len(k) < 2 or k in seen:
            continue
        seen.add(k)
        out.append(k[:100])
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def clean_text(s, limit=MAX_TITLE) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", str(s).lower()).strip()
    s = re.sub(r"[\s_-]+", "_", s)
    return s[:maxlen].strip("_") or "untitled"


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------
def _utf16_le_bom(s: str) -> bytes:
    return b"\xff\xfe" + s.encode("utf-16-le")


def build_exif_bytes(title: str, description: str,
                     keywords: list[str]) -> bytes:
    """EXIF: ImageDescription + XP* tags (Windows Explorer compatibility)."""
    return piexif.dump({
        "0th": {
            piexif.ImageIFD.ImageDescription:
                description.encode("utf-8")[:2048],
            piexif.ImageIFD.XPTitle: _utf16_le_bom(title + "\x00"),
            piexif.ImageIFD.XPComment: _utf16_le_bom(description + "\x00"),
            piexif.ImageIFD.XPKeywords:
                _utf16_le_bom(";".join(keywords) + "\x00"),
            piexif.ImageIFD.XPSubject: _utf16_le_bom(
                ";".join(keywords[:10]) + "\x00"),
        },
        "Exif": {}, "GPS": {}, "1st": {}, "Interop": {},
    })


def build_xmp_packet(title: str, description: str, keywords: list[str],
                     creator: str = "", copyright_: str = "") -> bytes:
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
    bag = "".join(f"<rdf:li>{esc(k)}</rdf:li>" for k in keywords)
    creators = (f'<dc:creator><rdf:Seq><rdf:li>{esc(creator)}</rdf:li>'
                f'</rdf:Seq></dc:creator>' if creator else "")
    rights = (f'<dc:rights><rdf:Alt><rdf:li xml:lang="x-default">'
              f'{esc(copyright_)}</rdf:li></rdf:Alt></dc:rights>'
              if copyright_ else "")
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999-02-22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{esc(title)}'
        '</rdf:li></rdf:Alt></dc:title>'
        f'<dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
        f'{esc(description)}</rdf:li></rdf:Alt></dc:description>'
        f'<dc:subject><rdf:Bag>{bag}</rdf:Bag></dc:subject>'
        f'{creators}{rights}'
        '</rdf:Description>'
        '<rdf:Description rdf:about="" '
        'xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/">'
        f'<Iptc4xmpCore:Keywords><rdf:Bag>{bag}</rdf:Bag>'
        '</Iptc4xmpCore:Keywords>'
        '</rdf:Description>'
        '</rdf:RDF></x:xmpmeta>'
        '<?xpacket end="w"?>'
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def webp_to_stock_jpeg(src, dst: str | Path, *, title: str, description: str,
                       keywords, creator: str = DEFAULT_CREATOR,
                       copyright: str = "") -> dict:
    """WebP (or PIL image) -> compliant stock JPEG. Returns metadata echo.

    Raises ValueError when title/keywords are missing — stock portals reject
    such files anyway, so failing fast is better than shipping dead weight.
    """
    title = clean_text(title, MAX_TITLE)
    description = clean_text(description, MAX_DESCRIPTION)
    kw = clean_keywords(keywords)
    if not title:
        raise ValueError("title is required for stock submission")
    if not kw:
        raise ValueError("at least one keyword is required")

    img = src if isinstance(src, Image.Image) else Image.open(src)
    with img:
        # flatten alpha onto white; force RGB (no CMYK, no palette)
        if img.mode in ("RGBA", "LA") or "transparency" in img.info:
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        icc = get_srgb_icc()
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 100 % JPEG, no chroma subsampling, progressive, sRGB-tagged
        img.save(dst, "JPEG", quality=100, subsampling=0, optimize=False,
                 progressive=True, icc_profile=icc or None,
                 exif=build_exif_bytes(title, description, kw),
                 xmp=build_xmp_packet(title, description, kw, creator,
                                      copyright))

    # true IPTC IIM records in the APP13 Photoshop block
    # (overwrite=True stops iptcinfo3 leaving a <file>~ backup behind)
    iptc = IPTCInfo(str(dst), force=True)
    iptc["object name"] = title
    iptc["caption/abstract"] = description
    iptc["keywords"] = list(kw)
    if creator:
        iptc["by-line"] = creator
    if copyright:
        iptc["copyright notice"] = copyright
    iptc.save({"overwrite": True})

    return {"title": title, "description": description,
            "keywords": kw, "n_keywords": len(kw)}


def cutout_to_dual_export(src, png_dst: str | Path, jpg_dst: str | Path, *,
                          title: str, description: str, keywords,
                          creator: str = DEFAULT_CREATOR,
                          copyright: str = "") -> dict:
    """Transparent cutout -> DUAL stock export.

    1. Transparent PNG  -> png_dst (alpha preserved, no metadata needed;
       Adobe Stock's PNG upload takes metadata from the companion CSV).
    2. Pure-white JPG   -> jpg_dst: alpha flattened onto solid white
       (255,255,255), 100 % quality sRGB JPEG with embedded ICC + full
       IPTC (2:05 / 2:120 / 2:25) + EXIF + XMP injection.

    Accepts a path or a PIL image. Returns a metadata echo dict.
    """
    title = clean_text(title, MAX_TITLE)
    description = clean_text(description, MAX_DESCRIPTION)
    kw = clean_keywords(keywords)
    if not title:
        raise ValueError("title is required for stock submission")
    if not kw:
        raise ValueError("at least one keyword is required")

    img = src if isinstance(src, Image.Image) else Image.open(src)
    with img:
        rgba = img.convert("RGBA")

        # --- 1. transparent PNG (alpha preserved) --------------------------
        png_dst = Path(png_dst)
        png_dst.parent.mkdir(parents=True, exist_ok=True)
        rgba.save(png_dst, "PNG", optimize=True)

        # --- 2. pure-white-background JPG + IPTC ---------------------------
        white = Image.new("RGB", rgba.size, (255, 255, 255))
        white.paste(rgba, mask=rgba.split()[-1])
        meta = webp_to_stock_jpeg(
            white, jpg_dst, title=title, description=description,
            keywords=kw, creator=creator, copyright=copyright)

    return {"title": title, "description": description,
            "keywords": kw, "n_keywords": len(kw),
            "png": str(png_dst), "jpg": str(Path(jpg_dst))}


def _dec(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, (list, tuple)):
        return [_dec(x) for x in v]
    return v


def read_back_metadata(path: str | Path) -> dict:
    """What a stock crawler / the contributor portal would see."""
    path = str(path)
    out: dict = {"file": path}
    iptc = IPTCInfo(path)
    out["iptc"] = {
        "ObjectName (2:05)": _dec(iptc["object name"]),
        "Caption-Abstract (2:120)": _dec(iptc["caption/abstract"]),
        "Keywords (2:25)": _dec(iptc["keywords"]),
        "By-line (2:80)": _dec(iptc["by-line"]),
        "Copyright (2:116)": _dec(iptc["copyright notice"]),
    }
    ex = piexif.load(path)["0th"]
    xp = ex.get(piexif.ImageIFD.XPKeywords)
    out["exif"] = {
        "ImageDescription": _dec(ex.get(piexif.ImageIFD.ImageDescription)),
        "XPKeywords": bytes(xp).decode("utf-16-le").strip("\x00") if xp else None,
    }
    raw = Path(path).read_bytes()
    out["xmp_present"] = b"xpacket" in raw
    img = Image.open(path)
    out["mode"], out["size"] = img.mode, img.size
    out["icc"] = ("embedded sRGB (%d bytes)" % len(img.info["icc_profile"])
                  if img.info.get("icc_profile") else "UNTAGGED (non-compliant!)")
    return out


if __name__ == "__main__":
    import json
    print("self-test: dummy image -> compliant JPEG -> read back ...")
    im = Image.new("RGB", (64, 64), (200, 30, 30))
    im.save("_mh_test.webp")
    meta = webp_to_stock_jpeg(
        "_mh_test.webp", "_mh_test.jpg",
        title="Red Fox In Snow",
        description="A red fox sitting in fresh snow at dusk.",
        keywords="animal, fox, WINTER,  snow;; wildlife,red fox,,fox",
        creator="Test Contributor", copyright="(c) 2026 Test")
    print("wrote:", json.dumps(meta, indent=1))
    print(json.dumps(read_back_metadata("_mh_test.jpg"), indent=1,
                     default=str))
    Path("_mh_test.webp").unlink(missing_ok=True)
    Path("_mh_test.jpg").unlink(missing_ok=True)
    print("OK")
