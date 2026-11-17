from __future__ import annotations

import os
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from PIL import ExifTags, Image


class MetadataService:
    """Extract safe file metadata.

    GPS handling added for image files only:
    - Reads EXIF GPSInfo when available.
    - Converts GPS DMS values to decimal latitude/longitude.
    - Adds a Google Maps link row for quick viewing.
    """

    IMAGE_EXTENSIONS = {
        ".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".heif", ".webp", ".png", ".bmp"
    }

    def extract(self, path: str) -> list[tuple[str, str]]:
        p = Path(path)
        stat = os.stat(path)
        rows: list[tuple[str, str]] = [
            ("[File System] Name", p.name),
            ("[File System] Path", str(p)),
            ("[File System] Size", f"{stat.st_size:,} bytes"),
        ]
        rows.extend(self.timestamp_rows(stat))

        if not self._is_image_path(p):
            rows.append(("[GPS] Status", "Skipped - GPS extraction is for image files only"))
            return rows

        try:
            with Image.open(path) as img:
                rows += [
                    ("[Image] Format", str(img.format or "N/A")),
                    ("[Image] Mode", str(img.mode or "N/A")),
                    ("[Image] Size", f"{img.width} x {img.height}"),
                ]

                exif = img.getexif()
                gps_rows = self._gps_rows_from_exif(exif)
                rows.extend(gps_rows)

                # Keep useful EXIF rows, but avoid dumping raw GPSInfo binary/dict rows twice.
                for tag_id, value in exif.items():
                    tag = ExifTags.TAGS.get(tag_id, str(tag_id))
                    if tag == "GPSInfo":
                        continue
                    rows.append((f"[EXIF] {tag}", self._safe_value(value)))

        except Exception as exc:
            rows.append(("[GPS] Status", f"Image metadata could not be read: {exc}"))

        return rows

    @staticmethod
    def format_time(epoch: float) -> str:
        """UTC with explicit offset, then local time, so reports are unambiguous."""
        utc = datetime.fromtimestamp(epoch, tz=timezone.utc)
        local = utc.astimezone()
        return f"{utc.strftime('%Y-%m-%d %H:%M:%S')} UTC  |  local {local.strftime('%Y-%m-%d %H:%M:%S %z')}"

    @classmethod
    def timestamp_rows(cls, stat) -> list[tuple[str, str]]:
        """Honest labels: ctime is creation time only on Windows (Python < 3.12);
        on Linux/macOS it is the inode-change time. Use st_birthtime when present."""
        birth = getattr(stat, "st_birthtime", None)
        if birth is not None:
            created = ("[File System] Created (birth time)", cls.format_time(birth))
        elif os.name == "nt":
            created = ("[File System] Created", cls.format_time(stat.st_ctime))
        else:
            created = ("[File System] Created", "Not available on this file system / OS")
        rows = [created]
        if os.name != "nt":
            rows.append(("[File System] Metadata changed (ctime)", cls.format_time(stat.st_ctime)))
        rows.append(("[File System] Modified (mtime)", cls.format_time(stat.st_mtime)))
        rows.append(("[File System] Accessed (atime)", cls.format_time(stat.st_atime)))
        return rows

    def extract_gps(self, path: str) -> tuple[float, float, str] | None:
        """Return (latitude, longitude, google_maps_url) for image files with EXIF GPS."""
        p = Path(path)
        if not self._is_image_path(p):
            return None
        try:
            with Image.open(path) as img:
                gps = self._gps_decimal_from_exif(img.getexif())
                if gps is None:
                    return None
                lat, lon = gps
                return lat, lon, self.google_maps_url(lat, lon)
        except Exception:
            return None

    def _gps_rows_from_exif(self, exif: Any) -> list[tuple[str, str]]:
        gps = self._gps_decimal_from_exif(exif)
        if gps is None:
            return [("[GPS] Status", "No GPS latitude/longitude found in image EXIF")]

        lat, lon = gps
        url = self.google_maps_url(lat, lon)
        return [
            ("[GPS] Latitude", f"{lat:.8f}"),
            ("[GPS] Longitude", f"{lon:.8f}"),
            ("[GPS] Google Maps", url),
        ]

    def _gps_decimal_from_exif(self, exif: Any) -> tuple[float, float] | None:
        try:
            gps_info_raw = exif.get_ifd(ExifTags.IFD.GPSInfo)
        except Exception:
            gps_info_raw = None

        if not gps_info_raw:
            return None

        gps_info = {
            ExifTags.GPSTAGS.get(key, key): value
            for key, value in gps_info_raw.items()
        }

        lat_values = gps_info.get("GPSLatitude")
        lat_ref = gps_info.get("GPSLatitudeRef")
        lon_values = gps_info.get("GPSLongitude")
        lon_ref = gps_info.get("GPSLongitudeRef")

        if not lat_values or not lon_values or not lat_ref or not lon_ref:
            return None

        lat = self._dms_to_decimal(lat_values)
        lon = self._dms_to_decimal(lon_values)
        if lat is None or lon is None:
            return None

        lat_ref_text = self._safe_value(lat_ref).upper()
        lon_ref_text = self._safe_value(lon_ref).upper()
        if lat_ref_text.startswith("S"):
            lat = -abs(lat)
        if lon_ref_text.startswith("W"):
            lon = -abs(lon)

        return lat, lon

    def _dms_to_decimal(self, values: Any) -> float | None:
        try:
            deg, minute, second = values
            return self._num(deg) + (self._num(minute) / 60.0) + (self._num(second) / 3600.0)
        except Exception:
            return None

    def _num(self, value: Any) -> float:
        if isinstance(value, tuple) and len(value) == 2:
            numerator, denominator = value
            return float(numerator) / float(denominator or 1)
        if isinstance(value, Fraction):
            return float(value)
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            return float(value.numerator) / float(value.denominator or 1)
        return float(value)

    def _is_image_path(self, path: Path) -> bool:
        return path.suffix.lower() in self.IMAGE_EXTENSIONS

    def _safe_value(self, value: Any) -> str:
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="replace")
            except Exception:
                return repr(value)
        return str(value)

    @staticmethod
    def google_maps_url(latitude: float, longitude: float) -> str:
        query = quote_plus(f"{latitude:.8f},{longitude:.8f}")
        return f"https://www.google.com/maps/search/?api=1&query={query}"
