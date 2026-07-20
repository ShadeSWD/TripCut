"""Запись EXIF (дата съёмки + GPS) в извлечённые кадры. JPEG — piexif,
PNG — eXIf-чанк через Pillow. Плюс mtime файла = дата съёмки."""
from __future__ import annotations

import os
from datetime import datetime
from fractions import Fraction

import piexif


def _deg_to_dms_rational(value: float):
    value = abs(value)
    d = int(value)
    m_f = (value - d) * 60
    m = int(m_f)
    s = round((m_f - m) * 60 * 10000)
    return ((d, 1), (m, 1), (s, 10000))


def build_exif(dt_local: datetime, lat: float | None = None, lon: float | None = None,
               ele: float | None = None, dt_utc: datetime | None = None,
               make: str = "", model: str = "") -> bytes:
    ds = dt_local.strftime("%Y:%m:%d %H:%M:%S")
    sub = f"{dt_local.microsecond // 1000:03d}" if dt_local.microsecond else None
    zeroth = {piexif.ImageIFD.DateTime: ds,
              piexif.ImageIFD.Software: "TripCut"}
    if make:
        zeroth[piexif.ImageIFD.Make] = make
    if model:
        zeroth[piexif.ImageIFD.Model] = model
    exif_ifd = {piexif.ExifIFD.DateTimeOriginal: ds,
                piexif.ExifIFD.DateTimeDigitized: ds}
    if sub:
        exif_ifd[piexif.ExifIFD.SubSecTimeOriginal] = sub

    gps_ifd = {}
    if lat is not None and lon is not None:
        gps_ifd = {
            piexif.GPSIFD.GPSVersionID: (2, 3, 0, 0),
            piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
            piexif.GPSIFD.GPSLatitude: _deg_to_dms_rational(lat),
            piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
            piexif.GPSIFD.GPSLongitude: _deg_to_dms_rational(lon),
        }
        if ele is not None:
            fr = Fraction(abs(ele)).limit_denominator(100)
            gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 0 if ele >= 0 else 1
            gps_ifd[piexif.GPSIFD.GPSAltitude] = (fr.numerator, fr.denominator)
        if dt_utc is not None:
            gps_ifd[piexif.GPSIFD.GPSDateStamp] = dt_utc.strftime("%Y:%m:%d")
            gps_ifd[piexif.GPSIFD.GPSTimeStamp] = ((dt_utc.hour, 1), (dt_utc.minute, 1),
                                                   (dt_utc.second, 1))
    return piexif.dump({"0th": zeroth, "Exif": exif_ifd, "GPS": gps_ifd})


def stamp_file(path: str, dt_local: datetime, lat: float | None = None,
               lon: float | None = None, ele: float | None = None,
               dt_utc: datetime | None = None, make: str = "", model: str = "") -> None:
    exif = build_exif(dt_local, lat, lon, ele, dt_utc, make, model)
    if path.lower().endswith((".jpg", ".jpeg")):
        piexif.insert(exif, path)
    elif path.lower().endswith(".png"):
        from PIL import Image
        im = Image.open(path)
        im.save(path, exif=exif)  # Pillow пишет eXIf-чанк
    ts = dt_local.timestamp()
    os.utime(path, (ts, ts))
