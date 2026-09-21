"""Validate and normalise an employee profile photo uploaded from the phone.

A raw phone photo is several MB, in an arbitrary orientation, and carries EXIF
metadata — including the GPS coordinates of where it was taken. None of that
should be stored or served, so every upload is reduced to a small, upright,
metadata-free JPEG before it touches disk.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import serializers

MAX_UPLOAD_BYTES = 10 * 1024 * 1024    # ordinary 12MP phone photos are 4-8 MB
# Pixel caps are judged from the header, before any decoding. JPEG is decoded at a
# reduced scale (see draft() below) so it can be big; other formats are decoded in
# full, so their cap is much lower (still above a 12MP phone screenshot).
MAX_PIXELS_JPEG = 50_000_000           # covers 48MP phone cameras
MAX_PIXELS_OTHER = 16_000_000
MAX_SIDE = 512                         # stored longest edge, in pixels
ALLOWED_FORMATS = {'JPEG', 'PNG', 'WEBP'}

_BAD_IMAGE = 'Upload a valid JPEG, PNG or WebP image.'


def normalize_photo(uploaded) -> bytes:
    """Return the uploaded image as a small, upright, metadata-free JPEG.

    Raises a DRF ValidationError (-> HTTP 400) if it isn't an acceptable image.
    """
    if uploaded.size > MAX_UPLOAD_BYTES:
        raise serializers.ValidationError(
            f'Photo is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).')

    try:
        Image.open(uploaded).verify()          # integrity check; invalidates the image
        uploaded.seek(0)
        image = Image.open(uploaded)           # so reopen it for real work
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError):
        raise serializers.ValidationError(_BAD_IMAGE)

    if image.format not in ALLOWED_FORMATS:
        raise serializers.ValidationError(_BAD_IMAGE)
    # Judge the size from the header BEFORE decoding any pixels: a few-KB file can
    # claim tens of megapixels, and decoding that is what exhausts memory.
    limit = MAX_PIXELS_JPEG if image.format == 'JPEG' else MAX_PIXELS_OTHER
    if image.width * image.height > limit:
        raise serializers.ValidationError('Image dimensions are too large.')

    if image.format == 'JPEG':
        # Decode straight to ~1/2, 1/4 or 1/8 scale (never below 2x the stored
        # size): a 48MP photo then costs a few MP of memory, not 150 MB.
        image.draft('RGB', (MAX_SIDE * 2, MAX_SIDE * 2))

    try:
        image.load()                           # the actual decode
    except (OSError, SyntaxError, ValueError):
        raise serializers.ValidationError(_BAD_IMAGE)

    # Honour the phone's rotation, THEN drop all metadata (nothing is carried
    # over when saving without an exif argument).
    image = ImageOps.exif_transpose(image)

    if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
        rgba = image.convert('RGBA')                       # flatten transparency onto white
        flat = Image.new('RGB', rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
        image = flat
    else:
        image = image.convert('RGB')

    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = BytesIO()
    image.save(out, 'JPEG', quality=85, optimize=True)
    return out.getvalue()
