from __future__ import annotations

import xml.etree.ElementTree as ET
import zlib
from typing import Any


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 70:
            break
    raise ValueError("truncated protobuf varint")


def _parse_elem(chunk: bytes) -> dict[str, Any]:
    item: dict[str, Any] = {}
    i = 0
    n = len(chunk)
    while i < n:
        key, i = _read_varint(chunk, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(chunk, i)
            mapping = {
                1: "id",
                2: "progress_ms",
                3: "mode",
                4: "fontsize",
                5: "color",
                8: "ctime",
                9: "weight",
                11: "pool",
                13: "attr",
            }
            if field in mapping:
                item[mapping[field]] = val
        elif wire == 2:
            length, i = _read_varint(chunk, i)
            raw = chunk[i : i + length]
            i += length
            if field == 6:
                item["mid_hash"] = raw.decode("utf-8", "replace")
            elif field == 7:
                item["content"] = raw.decode("utf-8", "replace")
            elif field == 10:
                item["action"] = raw.decode("utf-8", "replace")
            elif field == 12:
                item["id_str"] = raw.decode("utf-8", "replace")
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            break
    return item


def parse_seg_protobuf(data: bytes) -> list[dict[str, Any]]:
    if not data or data in (b"\x10\x01", b"\x08\x01"):
        return []
    items: list[dict[str, Any]] = []
    i = 0
    n = len(data)
    while i < n:
        try:
            key, i = _read_varint(data, i)
        except ValueError:
            break
        field, wire = key >> 3, key & 7
        if wire == 2:
            length, i = _read_varint(data, i)
            chunk = data[i : i + length]
            i += length
            if field == 1:
                elem = _parse_elem(chunk)
                if elem.get("content"):
                    items.append(elem)
        elif wire == 0:
            _, i = _read_varint(data, i)
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            break
    return items


def maybe_inflate(raw: bytes) -> bytes:
    if raw[:5].lstrip().startswith(b"<?xml") or raw[:1] == b"<":
        return raw
    for wbits in (-15, 15, 31):
        try:
            return zlib.decompress(raw, wbits)
        except zlib.error:
            continue
    return raw


def parse_xml_danmaku(raw: bytes) -> list[dict[str, Any]]:
    text = maybe_inflate(raw)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out: list[dict[str, Any]] = []
    for node in root.findall(".//d"):
        p = (node.get("p") or "").split(",")
        try:
            progress_ms = int(float(p[0]) * 1000) if p else 0
        except ValueError:
            progress_ms = 0
        out.append(
            {
                "progress_ms": progress_ms,
                "mode": int(p[1]) if len(p) > 1 and p[1].isdigit() else 1,
                "fontsize": int(p[2]) if len(p) > 2 and p[2].isdigit() else 25,
                "color": int(p[3]) if len(p) > 3 and p[3].isdigit() else 16777215,
                "ctime": int(p[4]) if len(p) > 4 and p[4].isdigit() else 0,
                "pool": int(p[5]) if len(p) > 5 and p[5].isdigit() else 0,
                "mid_hash": p[6] if len(p) > 6 else "",
                "id_str": p[7] if len(p) > 7 else "",
                "content": node.text or "",
            }
        )
    return out
