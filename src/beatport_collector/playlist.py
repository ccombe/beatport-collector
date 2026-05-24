"""Generate .m3u playlists and Rekordbox XML grouped by purchase date."""

from __future__ import annotations

import csv
import logging
import os
import urllib.parse
from collections import defaultdict
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

logger = logging.getLogger(__name__)


def _parse_purchase_date(raw: str) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def _extm3u_entry(filepath: str, title: str, artists: str) -> str:
    return f"#EXTINF:-1,{artists} - {title}\n{filepath}"


def _windows_path_to_file_uri(path: str) -> str:
    path = os.path.normpath(path)
    parts = path.split(os.sep)
    cleaned = []
    for p in parts:
        if p and p.endswith(":"):
            p = p.lower()
        cleaned.append(urllib.parse.quote(p, safe="/\\"))
    uri_path = "/".join(cleaned)
    if uri_path.startswith("/"):
        uri_path = uri_path.lstrip("/")
    return f"file://localhost/{uri_path}"


def create_playlists(
    matched_csv: str,
    output_dir: str = "playlists",
    rekordbox_xml: bool = False,
) -> dict[str, str]:
    """Create monthly/yearly .m3u playlists (+ optional Rekordbox XML).

    Returns dict of {playlist_name: filepath} for all files created.
    """
    os.makedirs(output_dir, exist_ok=True)

    with open(matched_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if r.get("Local File Path")]

    if not rows:
        raise RuntimeError("No matched tracks with local file paths found in CSV")

    monthly: dict[str, list[dict[str, str]]] = defaultdict(list)
    yearly: dict[str, list[dict[str, str]]] = defaultdict(list)

    for row in rows:
        dt = _parse_purchase_date(row.get("Purchase Date", ""))
        if dt is None:
            continue
        month_key = dt.strftime("%Y-%m")
        year_key = dt.strftime("%Y")
        monthly[month_key].append(row)
        yearly[year_key].append(row)

    created: dict[str, str] = {}

    for key in sorted(monthly.keys()):
        path = os.path.join(output_dir, f"{key}.m3u")
        _write_m3u(path, monthly[key])
        created[key] = path
        logger.info("Wrote %s (%d tracks)", path, len(monthly[key]))

    for key in sorted(yearly.keys()):
        filename = f"{key}-0.m3u"
        path = os.path.join(output_dir, filename)
        _write_m3u(path, yearly[key])
        created[filename] = path
        logger.info("Wrote %s (%d tracks)", path, len(yearly[key]))

    if rekordbox_xml:
        xml_path = os.path.join(output_dir, "rekordbox_playlists.xml")
        _write_rekordbox_xml(xml_path, rows, monthly, yearly)
        created["rekordbox_playlists.xml"] = xml_path
        logger.info("Wrote Rekordbox XML: %s (%d tracks)", xml_path, len(rows))

    print(f"\n  Created {len(created)} files in {output_dir}/")
    return created


def _write_m3u(path: str, tracks: list[dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for t in tracks:
            fp = t.get("Local File Path", "")
            title = t.get("Title", "")
            artists = t.get("Artists", "")
            if fp:
                f.write(_extm3u_entry(fp, title, artists) + "\n")


def _write_rekordbox_xml(
    xml_path: str,
    all_rows: list[dict[str, str]],
    monthly: dict[str, list[dict[str, str]]],
    yearly: dict[str, list[dict[str, str]]],
) -> None:
    """Generate a Rekordbox-compatible XML with all tracks and playlists."""

    root = Element("DJ_PLAYLISTS")
    root.set("Version", "1.0.0")

    product = SubElement(root, "PRODUCT")
    product.set("Name", "beatport_collector")
    product.set("Version", "1.0")
    product.set("Company", "")

    collection = SubElement(root, "COLLECTION")
    collection.set("Entries", str(len(all_rows)))

    track_id_map: dict[str, int] = {}
    for i, row in enumerate(all_rows, 1):
        track_id = i
        fp = row.get("Local File Path", "")
        track_id_map[fp] = track_id

        tr = SubElement(collection, "TRACK")
        tr.set("TrackID", str(track_id))
        tr.set("Name", row.get("Title", ""))
        tr.set("Artist", row.get("Artists", ""))
        tr.set("Album", row.get("Release Title", ""))
        tr.set("Location", _windows_path_to_file_uri(fp))
        tr.set("TotalTime", "0")
        tr.set("Kind", os.path.splitext(fp)[1].lstrip(".") if fp else "mp3")

    playlists = SubElement(root, "PLAYLISTS")
    root_node = SubElement(playlists, "NODE")
    root_node.set("Type", "0")
    root_node.set("Name", "ROOT")

    def _unique_tracks(tracks: list[dict[str, str]]) -> list[dict[str, str]]:
        seen: set[str] = set()
        result: list[dict[str, str]] = []
        for t in tracks:
            fp = t.get("Local File Path", "")
            if fp and fp not in seen:
                seen.add(fp)
                result.append(t)
        return result

    for year_key in sorted(yearly.keys()):
        year_folder = SubElement(root_node, "NODE")
        year_folder.set("Type", "0")
        year_folder.set("Name", year_key)

        year_tracks = _unique_tracks(yearly[year_key])
        year_pl = SubElement(year_folder, "NODE")
        year_pl.set("Type", "1")
        year_pl.set("Name", f"{year_key} (Full Year)")
        year_pl.set("KeyType", "0")
        year_pl.set("Entries", str(len(year_tracks)))
        for row in year_tracks:
            fp = row.get("Local File Path", "")
            tid = track_id_map.get(fp)
            if tid:
                tr = SubElement(year_pl, "TRACK")
                tr.set("Key", str(tid))

        for month_key in sorted(monthly.keys()):
            if month_key.startswith(year_key):
                month_tracks = _unique_tracks(monthly[month_key])
                month_pl = SubElement(year_folder, "NODE")
                month_pl.set("Type", "1")
                month_pl.set("Name", month_key)
                month_pl.set("KeyType", "0")
                month_pl.set("Entries", str(len(month_tracks)))
                for row in month_tracks:
                    fp = row.get("Local File Path", "")
                    tid = track_id_map.get(fp)
                    if tid:
                        tr = SubElement(month_pl, "TRACK")
                        tr.set("Key", str(tid))

    rough_string = tostring(root, encoding="unicode")
    reparsed = minidom.parseString(rough_string)
    pretty = reparsed.toprettyxml(indent="  ", encoding="utf-8")

    with open(xml_path, "wb") as f:
        f.write(pretty)
