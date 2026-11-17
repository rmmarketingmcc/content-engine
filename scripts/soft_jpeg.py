"""Photo (HEIC from an iPhone too) to an upright, resized JPEG, decoded and scaled on the CPU only.

Used instead of macOS `sips`, which decodes and scales on the Mac's hardware scaler: a long night of photo work on
that chip can crash some Macs. pillow-heif decodes HEIC in software, Pillow scales it.
"""
import pathlib

from PIL import Image, ImageOps
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:  # a machine without photo work (the posting server) only imports this for the Check line notes
    pass


def to_jpeg(src, out, max_px: int, quality: int = 92) -> bool:
    """Writes `out` (longest side at most max_px, the phone's rotation baked in, colour profile kept). False when the
    file can't be read, so a caller can skip it."""
    try:
        with Image.open(src) as im:
            icc = im.info.get("icc_profile")
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
            pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
            im.save(out, "JPEG", quality=quality, icc_profile=icc)
        return True
    except Exception:
        return False
