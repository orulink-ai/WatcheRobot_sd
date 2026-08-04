#!/usr/bin/env python3
"""Build, validate and package WatcheRobot SD resources.

The generated bundle intentionally matches the formats consumed by the current
ESP32 firmware: AnimPack v2 (RGB565 big-endian), raw PCM s16le/24 kHz/mono,
and the existing behavior action JSON parser contract.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import shutil
import struct
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image, ImageSequence
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
POLICY_PATH = ROOT / "config" / "resource-policy.json"
DEFAULT_SOURCE = ROOT / "official" / "source"
DEFAULT_PCM = ROOT / "official" / "device-input" / "sound"
DEFAULT_BUNDLE = ROOT / "build" / "current" / "bundle"
DEFAULT_DESKTOP = ROOT / "official" / "desktop"
DEFAULT_DIST = ROOT / "dist"
GITHUB_RELEASE_BASE = "https://github.com/orulink-ai/WatcheRobot_sd/releases/download"
TOS_PUBLIC_BASE = "https://erroright.tos-cn-guangzhou.volces.com/WatcherRobot/sd"

PACK_MAGIC = b"ANPK"
PACK_VERSION = 2
PACK_HEADER_FMT = "<4sHHHHBBHIII"
FRAME_DESC_FMT = "<IIHH"
FRAME_FLAG_INDEXED8 = 0x0001
DEVICE_CATALOG_MAX_BYTES = 128 * 1024
DEVICE_PATH_MAX_BYTES = 192
DEVICE_DISPLAY_NAME_MAX_BYTES = 192


class ResourceError(ValueError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResourceError(f"Invalid JSON {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


ASSET_EXTENSIONS = {
    "anim": ".animpack",
    "action": ".json",
    "sfx": ".pcm",
}

ASSET_FORMATS = {
    "anim": "animpack-v2",
    "action": "firmware-action-json-v1",
    "sfx": "pcm-s16le-24khz-mono",
}


def store_asset_object(bundle: Path, source: Path, kind: str) -> dict[str, Any]:
    if kind not in ASSET_EXTENSIONS:
        raise ResourceError(f"Unsupported asset kind: {kind}")
    digest = sha256_file(source)
    target = bundle / "assets" / ("actions" if kind == "action" else kind) / (
        digest + ASSET_EXTENSIONS[kind]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size != source.stat().st_size or sha256_file(target) != digest:
            raise ResourceError(f"Asset hash collision or corrupt existing object: {target}")
    else:
        shutil.copyfile(source, target)
    return {
        "kind": kind,
        "sha256": digest,
        "size": source.stat().st_size,
        "format": ASSET_FORMATS[kind],
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_policy() -> dict[str, Any]:
    return read_json(POLICY_PATH)


def schema_validate(document: Any, schema_name: str, label: str) -> None:
    schema = read_json(SCHEMAS / schema_name)
    registry = Registry()
    for schema_path in SCHEMAS.glob("*.schema.json"):
        candidate = read_json(schema_path)
        if isinstance(candidate, dict) and isinstance(candidate.get("$id"), str):
            registry = registry.with_resource(candidate["$id"], Resource.from_contents(candidate))
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
            registry=registry,
        ).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors[:8]
        )
        raise ResourceError(f"{label} does not match {schema_name}: {details}")


def rgba_to_rgb565(image: Image.Image) -> bytes:
    payload = bytearray()
    rgba = image.convert("RGBA").tobytes()
    for offset in range(0, len(rgba), 4):
        red, green, blue, alpha = rgba[offset : offset + 4]
        if alpha != 255:
            red = red * alpha // 255
            green = green * alpha // 255
            blue = blue * alpha // 255
        value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
        payload.extend(struct.pack(">H", value))
    return bytes(payload)


def encode_indexed8(payload: bytes) -> bytes | None:
    palette: list[bytes] = []
    lookup: dict[bytes, int] = {}
    indices = bytearray()
    for offset in range(0, len(payload), 2):
        color = payload[offset : offset + 2]
        index = lookup.get(color)
        if index is None:
            if len(palette) >= 256:
                return None
            index = len(palette)
            palette.append(color)
            lookup[color] = index
        indices.append(index)
    return struct.pack("<H", len(palette)) + b"".join(palette) + bytes(indices)


def read_gif(path: Path, policy: dict[str, Any]) -> list[tuple[Image.Image, int]]:
    expected = (policy["display"]["width"], policy["display"]["height"])
    max_frames = policy["limits"]["frames_per_expression"]
    frames: list[tuple[Image.Image, int]] = []
    try:
        with Image.open(path) as image:
            if image.format != "GIF":
                raise ResourceError(f"{path.name} is not a GIF file")
            fallback = max(20, int(image.info.get("duration", 100) or 100))
            for index, frame in enumerate(ImageSequence.Iterator(image)):
                converted = frame.convert("RGBA")
                if converted.size != expected:
                    raise ResourceError(
                        f"{path.name} frame {index} is {converted.width}x{converted.height}; "
                        f"device requires {expected[0]}x{expected[1]}"
                    )
                delay = max(20, int(frame.info.get("duration", fallback) or fallback))
                frames.append((converted.copy(), delay))
    except OSError as exc:
        raise ResourceError(f"Unable to decode GIF {path}: {exc}") from exc
    if not frames:
        raise ResourceError(f"GIF has no frames: {path}")
    if len(frames) > max_frames:
        raise ResourceError(f"{path.name} has {len(frames)} frames; limit is {max_frames}")
    return frames


def write_animpack(
    target: Path,
    frames: list[tuple[Image.Image, int]],
    fps: int,
    loop: bool,
    force_rgb565: bool,
) -> dict[str, int]:
    width, height = frames[0][0].size
    raw_frames = [rgba_to_rgb565(frame) for frame, _ in frames]
    encoded: list[tuple[bytes, int]] = []
    for raw in raw_frames:
        indexed = None if force_rgb565 else encode_indexed8(raw)
        encoded.append((indexed, FRAME_FLAG_INDEXED8) if indexed is not None else (raw, 0))

    header_size = struct.calcsize(PACK_HEADER_FMT)
    descriptor_size = struct.calcsize(FRAME_DESC_FMT)
    payload_offset = header_size + descriptor_size * len(frames)
    cursor = 0
    descriptors: list[bytes] = []
    for (payload, flags), (_, delay) in zip(encoded, frames):
        descriptors.append(struct.pack(FRAME_DESC_FMT, cursor, len(payload), delay, flags))
        cursor += len(payload)
    header = struct.pack(
        PACK_HEADER_FMT,
        PACK_MAGIC,
        PACK_VERSION,
        width,
        height,
        len(frames),
        1 if loop else 0,
        0,
        max(1, round(1000 / fps)),
        header_size,
        payload_offset,
        width * height * 2,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        handle.write(header)
        handle.writelines(descriptors)
        for payload, _ in encoded:
            handle.write(payload)
    return {
        "width": width,
        "height": height,
        "frames": len(frames),
        "indexed_frames": sum(bool(flags & FRAME_FLAG_INDEXED8) for _, flags in encoded),
    }


def decode_animpack(path: Path) -> tuple[dict[str, int], list[bytes]]:
    data = path.read_bytes()
    header_size = struct.calcsize(PACK_HEADER_FMT)
    descriptor_size = struct.calcsize(FRAME_DESC_FMT)
    if len(data) < header_size:
        raise ResourceError(f"AnimPack header is truncated: {path}")
    (
        magic,
        version,
        width,
        height,
        frame_count,
        loop,
        _reserved,
        delay,
        toc_offset,
        payload_offset,
        raw_size,
    ) = struct.unpack_from(PACK_HEADER_FMT, data)
    if magic != PACK_MAGIC or version != PACK_VERSION:
        raise ResourceError(f"Unsupported AnimPack header: {path}")
    if width <= 0 or height <= 0 or frame_count <= 0 or raw_size != width * height * 2:
        raise ResourceError(f"Invalid AnimPack dimensions/count: {path}")
    if toc_offset != header_size or payload_offset != header_size + frame_count * descriptor_size:
        raise ResourceError(f"Invalid AnimPack offsets: {path}")
    frames: list[bytes] = []
    for index in range(frame_count):
        frame_offset, frame_size, frame_delay, flags = struct.unpack_from(
            FRAME_DESC_FMT, data, toc_offset + index * descriptor_size
        )
        start = payload_offset + frame_offset
        end = start + frame_size
        if end > len(data) or frame_delay <= 0 or flags & ~FRAME_FLAG_INDEXED8:
            raise ResourceError(f"Invalid AnimPack frame {index}: {path}")
        payload = data[start:end]
        if not flags & FRAME_FLAG_INDEXED8:
            if len(payload) != raw_size:
                raise ResourceError(f"Invalid RGB565 frame size in {path}")
            frames.append(payload)
            continue
        if len(payload) < 4:
            raise ResourceError(f"Invalid indexed frame in {path}")
        palette_count = struct.unpack_from("<H", payload)[0]
        palette_end = 2 + palette_count * 2
        indices = payload[palette_end:]
        if not 1 <= palette_count <= 256 or palette_end > len(payload) or len(indices) != width * height:
            raise ResourceError(f"Invalid indexed palette in {path}")
        palette = payload[2:palette_end]
        raw = bytearray()
        for palette_index in indices:
            if palette_index >= palette_count:
                raise ResourceError(f"Out-of-range palette index in {path}")
            start_color = palette_index * 2
            raw.extend(palette[start_color : start_color + 2])
        frames.append(bytes(raw))
    return {
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "loop": loop,
        "delay": delay,
    }, frames


def device_asset_path(asset: dict[str, Any]) -> str:
    """Return the exact content-addressed path resolved by the ESP32 catalog."""
    kind = asset["kind"]
    directory = "actions" if kind == "action" else kind
    return f"/sdcard/watche/assets/{directory}/{asset['sha256']}{ASSET_EXTENSIONS[kind]}"


def validate_device_playback_contract(
    catalog: dict[str, Any], bundle: Path, policy: dict[str, Any]
) -> dict[str, int]:
    """Validate every official expression using the ESP32 dynamic-SD contract.

    This deliberately does not consult the firmware's compiled animation registry.
    A newly published resource is required to be playable solely from its catalog ID
    and content-addressed SD objects.
    """
    expressions = catalog["expressions"]
    if (bundle / "official_catalog.json").stat().st_size > DEVICE_CATALOG_MAX_BYTES:
        raise ResourceError("official_catalog.json exceeds the ESP32 catalog size limit")

    id_pattern = re.compile(policy["resource_id"]["pattern"])
    max_id_bytes = policy["resource_id"]["max_bytes"]
    fixed_ids = set(policy["fixed_states"])
    dynamic_count = 0
    total_first_frames = 0
    for entry in expressions:
        resource_id = entry["id"]
        if not id_pattern.fullmatch(resource_id) or len(resource_id.encode("utf-8")) > max_id_bytes:
            raise ResourceError(f"ESP32 rejects resource ID: {resource_id}")
        if len(entry["display_name"].encode("utf-8")) > DEVICE_DISPLAY_NAME_MAX_BYTES:
            raise ResourceError(f"ESP32 display name is too long: {resource_id}")
        if len(entry["source_record_id"].encode("utf-8")) > 128:
            raise ResourceError(f"ESP32 source record ID is too long: {resource_id}")
        if resource_id not in fixed_ids:
            dynamic_count += 1

        for kind, asset in entry["assets"].items():
            expected_kind = {"animation": "anim", "action": "action", "sound": "sfx"}[kind]
            if asset["kind"] != expected_kind:
                raise ResourceError(f"Unexpected {kind} kind for {resource_id}")
            resolved = device_asset_path(asset)
            if len(resolved.encode("utf-8")) >= DEVICE_PATH_MAX_BYTES:
                raise ResourceError(f"ESP32 asset path is too long: {resource_id}/{kind}")
            path = bundle / "assets" / ("actions" if asset["kind"] == "action" else asset["kind"]) / (
                asset["sha256"] + ASSET_EXTENSIONS[asset["kind"]]
            )
            if not path.is_file() or path.stat().st_size != asset["size"] or sha256_file(path) != asset["sha256"]:
                raise ResourceError(f"ESP32 asset resolution failed: {resource_id}/{kind}")

        animation = entry["assets"]["animation"]
        animation_path = bundle / "assets" / "anim" / f"{animation['sha256']}.animpack"
        header, frames = decode_animpack(animation_path)
        if (
            header["width"] != policy["display"]["width"]
            or header["height"] != policy["display"]["height"]
            or header["frame_count"] > policy["limits"]["frames_per_expression"]
            or not frames
            or len(frames[0]) != header["width"] * header["height"] * 2
        ):
            raise ResourceError(f"ESP32 dynamic playback contract failed: {resource_id}")
        total_first_frames += 1
    return {"dynamic_expressions": dynamic_count, "first_frames": total_first_frames}


def normalize_action(source: Path) -> tuple[dict[str, Any], list[str]]:
    document = read_json(source)
    if not isinstance(document, dict):
        raise ResourceError(f"{source.name}: action root must be an object")
    if document.get("kind") == "watcher.motion-studio.action-draft":
        document = action_draft_to_firmware(document, source.name)
    fps = require_integer(document.get("fps"), f"{source.name}.fps", 1)
    frame_start = require_integer(document.get("frame_start"), f"{source.name}.frame_start", 0)
    frame_end = require_integer(document.get("frame_end"), f"{source.name}.frame_end", frame_start)
    objects = document.get("animated_objects")
    if not isinstance(objects, list):
        raise ResourceError(f"{source.name}.animated_objects must be an array")

    normalized_objects: list[dict[str, Any]] = []
    adjustments: list[str] = []
    for object_index, animated_object in enumerate(objects):
        if not isinstance(animated_object, dict) or not isinstance(animated_object.get("keyframe_data"), list):
            raise ResourceError(f"{source.name}.animated_objects[{object_index}] is invalid")
        by_axis: dict[str, list[dict[str, int]]] = {"z": [], "x": []}
        for frame_index, keyframe in enumerate(animated_object["keyframe_data"]):
            if not isinstance(keyframe, dict) or keyframe.get("active_axis") not in ("z", "x"):
                continue
            axis = keyframe["active_axis"]
            frame_number = require_integer(
                keyframe.get("frame_number"),
                f"{source.name}.keyframe[{frame_index}].frame_number",
                frame_start,
            )
            if frame_number > frame_end:
                raise ResourceError(f"{source.name}: keyframe {frame_number} exceeds frame_end {frame_end}")
            raw_angle = keyframe.get("rotation_angle")
            if isinstance(raw_angle, bool) or not isinstance(raw_angle, (int, float)) or not math.isfinite(raw_angle):
                raise ResourceError(f"{source.name}: rotation_angle must be numeric")
            rounded = int(round(raw_angle))
            minimum, maximum = ((0, 180) if axis == "z" else (100, 140))
            clamped = min(max(rounded, minimum), maximum)
            if clamped != raw_angle:
                adjustments.append(f"{axis}@{frame_number}:{raw_angle}->{clamped}")
            by_axis[axis].append(
                {"frame_number": frame_number, "active_axis": axis, "rotation_angle": clamped}
            )
        for axis, keyframes in by_axis.items():
            if not keyframes:
                continue
            deduplicated = {item["frame_number"]: item for item in keyframes}
            normalized_objects.append(
                {
                    "object_name": "body_x" if axis == "z" else "head_y",
                    "object_type": "MESH" if axis == "z" else "EMPTY",
                    "action_name": source.stem,
                    "keyframe_data": [deduplicated[key] for key in sorted(deduplicated)],
                }
            )
    if not normalized_objects:
        raise ResourceError(f"{source.name}: action has no playable x/z keyframes")
    result = {
        "scene_name": source.stem,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "fps": fps,
        "animated_objects": normalized_objects,
    }
    schema_validate(result, "action.schema.json", source.name)
    return result, adjustments


def action_draft_to_firmware(document: dict[str, Any], source_name: str) -> dict[str, Any]:
    if document.get("version") != 1 or not isinstance(document.get("tracks"), dict):
        raise ResourceError(f"{source_name}: unsupported action draft")
    objects: list[dict[str, Any]] = []
    frame_end = 0
    for track, axis, name, object_type in (
        ("xDeg", "z", "body_x", "MESH"),
        ("yDeg", "x", "head_y", "EMPTY"),
    ):
        items = document["tracks"].get(track, [])
        if not isinstance(items, list):
            raise ResourceError(f"{source_name}: tracks.{track} must be an array")
        keyframes = []
        for item in items:
            if not isinstance(item, dict):
                raise ResourceError(f"{source_name}: invalid {track} keyframe")
            at_ms = require_integer(item.get("timeMs"), f"{source_name}.{track}.timeMs", 0)
            keyframes.append(
                {
                    "frame_number": at_ms,
                    "active_axis": axis,
                    "rotation_angle": item.get("angleDeg"),
                }
            )
            frame_end = max(frame_end, at_ms)
        if keyframes:
            objects.append(
                {
                    "object_name": name,
                    "object_type": object_type,
                    "action_name": str(document.get("label") or source_name),
                    "keyframe_data": keyframes,
                }
            )
    return {
        "scene_name": str(document.get("label") or source_name),
        "frame_start": 0,
        "frame_end": frame_end,
        "fps": 1000,
        "animated_objects": objects,
    }


def require_integer(value: Any, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ResourceError(f"{field} must be an integer")
    result = int(value)
    if result < minimum:
        raise ResourceError(f"{field} must be >= {minimum}")
    return result


def write_preview(source: Path, target: Path) -> None:
    frames: list[Image.Image] = []
    durations: list[int] = []
    with Image.open(source) as image:
        fallback = max(20, int(image.info.get("duration", 100) or 100))
        for frame in ImageSequence.Iterator(image):
            frames.append(frame.convert("RGBA").copy())
            durations.append(max(20, int(frame.info.get("duration", fallback) or fallback)))
    target.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        target,
        "WEBP",
        save_all=len(frames) > 1,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        quality=68,
        method=4,
    )


def source_snapshot(source: Path) -> dict[str, Any]:
    snapshot_path = source / "feishu-snapshot.json"
    if not snapshot_path.is_file():
        raise ResourceError(f"Source snapshot is missing: {snapshot_path}")
    snapshot = read_json(snapshot_path)
    schema_validate(snapshot, "source-snapshot.schema.json", "feishu-snapshot.json")
    seen_ids: set[str] = set()
    seen_records: set[str] = set()
    for record in snapshot["records"]:
        if record["resource_id"] in seen_ids:
            raise ResourceError(f"Duplicate resource_id: {record['resource_id']}")
        if record["source_record_id"] in seen_records:
            raise ResourceError(f"Duplicate source_record_id: {record['source_record_id']}")
        seen_ids.add(record["resource_id"])
        seen_records.add(record["source_record_id"])
    return snapshot


def gif_has_loop_extension(path: Path) -> bool:
    """Read AnimPack's technical loop bit from the GIF rather than Base metadata."""
    with Image.open(path) as image:
        return "loop" in image.info


def build_resources(
    source: Path,
    pcm_root: Path,
    bundle: Path,
    desktop: Path,
    version: str,
) -> dict[str, Any]:
    policy = load_policy()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise ResourceError(f"Invalid resource version: {version}")
    snapshot = source_snapshot(source)
    records = sorted(snapshot["records"], key=lambda item: item["order"])
    if bundle.exists():
        shutil.rmtree(bundle)
    if desktop.exists():
        shutil.rmtree(desktop)
    bundle.mkdir(parents=True)
    desktop.mkdir(parents=True)
    staging = bundle / ".build"
    staging.mkdir()

    catalog: list[dict[str, Any]] = []
    adjustments: dict[str, list[str]] = {}
    fps = policy["display"]["default_fps"]
    for record in records:
        resource_id = record["resource_id"]
        gif_path = source / "gif" / f"{resource_id}.gif"
        if not gif_path.is_file():
            raise ResourceError(f"Source GIF is missing for {resource_id}")
        frames = read_gif(gif_path, policy)
        animation_temp = staging / f"{resource_id}.animpack"
        write_animpack(
            animation_temp,
            frames,
            fps,
            gif_has_loop_extension(gif_path),
            resource_id in policy["force_rgb565"],
        )
        assets: dict[str, Any] = {
            "animation": store_asset_object(bundle, animation_temp, "anim")
        }
        entry: dict[str, Any] = {
            "id": resource_id,
            "display_name": record["display_name"],
            "source_record_id": record["source_record_id"],
            "order": record["order"],
            "assets": assets,
        }
        action_path = source / "actions" / f"{resource_id}.json"
        if record.get("action") is not None:
            if not action_path.is_file():
                raise ResourceError(f"Action attachment is declared but missing for {resource_id}")
            action, changed = normalize_action(action_path)
            action_temp = staging / f"{resource_id}.json"
            write_json(action_temp, action)
            assets["action"] = store_asset_object(bundle, action_temp, "action")
            if changed:
                adjustments[resource_id] = changed
        sound_path = pcm_root / f"{resource_id}.pcm"
        if record.get("sound") is not None:
            if not sound_path.is_file():
                raise ResourceError(f"PCM conversion output is missing for {resource_id}")
            assets["sound"] = store_asset_object(bundle, sound_path, "sfx")
        catalog.append(entry)

    catalog_document = {
        "schema_version": 2,
        "format": "watche-official-catalog",
        "expressions": catalog,
    }
    schema_validate(catalog_document, "resource-catalog.schema.json", "official_catalog.json")
    write_json(bundle / "official_catalog.json", catalog_document)
    fixed_states = {
        "schema_version": 1,
        "states": {state: state for state in policy["fixed_states"]},
    }
    schema_validate(fixed_states, "fixed-states.schema.json", "fixed_states.json")
    write_json(bundle / "fixed_states.json", fixed_states)

    desktop_entries = []
    for record, entry in zip(records, catalog):
        preview = desktop / "previews" / f"{entry['id']}.webp"
        write_preview(source / "gif" / f"{entry['id']}.gif", preview)
        creator: dict[str, Any] = {}
        if "action" in entry["assets"]:
            reference = entry["assets"]["action"]
            relative = f"actions/{entry['id']}.json"
            target = desktop / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                bundle / "assets" / "actions" / f"{reference['sha256']}.json",
                target,
            )
            creator["action"] = {"path": relative, **reference}
        if "sound" in entry["assets"]:
            reference = entry["assets"]["sound"]
            relative = f"sounds/{entry['id']}.pcm"
            target = desktop / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                bundle / "assets" / "sfx" / f"{reference['sha256']}.pcm",
                target,
            )
            creator["sound"] = {"path": relative, **reference}
        device = {
            "image_name": entry["id"],
            "assets": entry["assets"],
        }
        desktop_entries.append(
            {
                "id": entry["id"],
                "display_name": record["display_name"],
                "source_label": record["source_label"],
                "source_record_id": record["source_record_id"],
                "preview": f"previews/{entry['id']}.webp",
                "preview_sha256": sha256_file(preview),
                "order": entry["order"],
                "creator": creator,
                "device": device,
            }
        )
    content_digest = hashlib.sha256()
    for item in desktop_entries:
        content_digest.update(item["id"].encode())
        content_digest.update(item["display_name"].encode())
        content_digest.update(item["preview_sha256"].encode())
        content_digest.update(
            json.dumps(item["creator"], sort_keys=True, separators=(",", ":")).encode()
        )
    desktop_catalog = {
        "schema_version": 3,
        "format": "watche-desktop-resource-catalog",
        "version": version,
        "content_hash": content_digest.hexdigest(),
        "expressions": desktop_entries,
    }
    schema_validate(desktop_catalog, "desktop-catalog.schema.json", "desktop_catalog.json")
    write_json(desktop / "desktop_catalog.json", desktop_catalog)

    shutil.rmtree(staging)
    manifest = write_resource_manifest(bundle, version, policy, catalog)
    validation = validate_resources(source, bundle, desktop)
    return {
        "version": version,
        "expressions": len(catalog),
        "actions": sum("action" in item["assets"] for item in catalog),
        "sounds": sum("sound" in item["assets"] for item in catalog),
        "frames": validation["frames"],
        "bundle_sha256": manifest["bundle_sha256"],
        "action_adjustments": adjustments,
    }


def bundle_files(bundle: Path, include_manifest: bool = True) -> list[Path]:
    files = [path for path in bundle.rglob("*") if path.is_file()]
    if not include_manifest:
        files = [path for path in files if path.name != "resource_manifest.json"]
    return sorted(files, key=lambda path: path.relative_to(bundle).as_posix())


def desktop_files(desktop: Path) -> list[Path]:
    return sorted(
        (path for path in desktop.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(desktop).as_posix(),
    )


def write_deterministic_tar_gz(archive: Path, root: Path, files: list[Path], prefix: str) -> None:
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        tar_path = Path(temporary) / "resources.tar"
        with tarfile.open(tar_path, "w", format=tarfile.USTAR_FORMAT) as tar:
            for path in files:
                relative = path.relative_to(root).as_posix()
                info = tar.gettarinfo(str(path), arcname=relative)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
        with tar_path.open("rb") as source, archive.open("wb") as output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=output,
                mtime=0,
                compresslevel=9,
            ) as compressed:
                shutil.copyfileobj(source, compressed)


def calculate_bundle_hash(bundle: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(bundle).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(sha256_file(path).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def write_resource_manifest(
    bundle: Path,
    version: str,
    policy: dict[str, Any],
    catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    files = bundle_files(bundle, include_manifest=False)
    entries = [
        {
            "path": path.relative_to(bundle).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]
    manifest = {
        "schema_version": 2,
        "product": policy["product"],
        "bundle_id": "adepto-official",
        "bundle_version": version,
        "layout_revision": 2,
        "generated_at": utc_now(),
        "compatibility": {
            "layout": "/watche",
            "protocol": "WRSD/2",
            "animation": {
                "format": "animpack-v2",
                "width": policy["display"]["width"],
                "height": policy["display"]["height"],
                "pixel_format": policy["display"]["pixel_format"],
                "byte_order": policy["display"]["byte_order"],
            },
            "audio": policy["audio"],
            "action": {"format": "firmware-action-json-v1"},
        },
        "contents": {
            "assets": {
                "root": "assets",
                "format": "animpack-v2",
                "object_count": sum(
                    len(entry["assets"])
                    for entry in catalog
                ),
            },
            "catalog": {
                "path": "official_catalog.json",
                "format": "watche-official-catalog-v2",
                "count": len(catalog),
            },
            "fixed_states": {
                "path": "fixed_states.json",
                "format": "watche-fixed-states-v1",
            },
        },
        "files": entries,
        "bundle_sha256": calculate_bundle_hash(bundle, files),
    }
    schema_validate(manifest, "resource-manifest.schema.json", "resource_manifest.json")
    write_json(bundle / "resource_manifest.json", manifest)
    return manifest


def validate_resources(source: Path, bundle: Path, desktop: Path | None) -> dict[str, int]:
    policy = load_policy()
    snapshot = source_snapshot(source)
    catalog = read_json(bundle / "official_catalog.json")
    schema_validate(catalog, "resource-catalog.schema.json", "official_catalog.json")
    manifest = read_json(bundle / "resource_manifest.json")
    schema_validate(manifest, "resource-manifest.schema.json", "resource_manifest.json")
    fixed_states = read_json(bundle / "fixed_states.json")
    schema_validate(fixed_states, "fixed-states.schema.json", "fixed_states.json")
    if set(fixed_states["states"]) != set(policy["fixed_states"]):
        raise ResourceError("fixed_states.json must contain exactly the eight fixed states")

    source_by_id = {record["resource_id"]: record for record in snapshot["records"]}
    catalog_by_id = {entry["id"]: entry for entry in catalog["expressions"]}
    if set(source_by_id) != set(catalog_by_id):
        raise ResourceError("Source snapshot and device catalog resource IDs do not match")
    device_contract = validate_device_playback_contract(catalog, bundle, policy)
    total_frames = 0
    for resource_id, entry in catalog_by_id.items():
        if entry["source_record_id"] != source_by_id[resource_id]["source_record_id"]:
            raise ResourceError(f"Record association mismatch for {resource_id}")
        source_frames = read_gif(source / "gif" / f"{resource_id}.gif", policy)
        animation = entry["assets"]["animation"]
        animation_path = bundle / "assets" / "anim" / f"{animation['sha256']}.animpack"
        if animation_path.stat().st_size != animation["size"]:
            raise ResourceError(f"Animation size mismatch for {resource_id}")
        header, frames = decode_animpack(animation_path)
        if len(frames) != len(source_frames):
            raise ResourceError(f"Frame count mismatch for {resource_id}")
        for index, ((image, _), actual) in enumerate(zip(source_frames, frames)):
            if actual != rgba_to_rgb565(image):
                raise ResourceError(f"RGB565 pixel/byte-order mismatch: {resource_id} frame {index}")
        total_frames += len(frames)
        if "action" in entry["assets"]:
            reference = entry["assets"]["action"]
            action_path = bundle / "assets" / "actions" / f"{reference['sha256']}.json"
            action = read_json(action_path)
            schema_validate(action, "action.schema.json", str(action_path))
        if "sound" in entry["assets"]:
            reference = entry["assets"]["sound"]
            sound = bundle / "assets" / "sfx" / f"{reference['sha256']}.pcm"
            if not sound.is_file() or sound.stat().st_size <= 0 or sound.stat().st_size % 2:
                raise ResourceError(f"Invalid raw PCM file for {resource_id}")

    actual_files = bundle_files(bundle, include_manifest=False)
    expected_files = {item["path"]: item for item in manifest["files"]}
    if set(expected_files) != {path.relative_to(bundle).as_posix() for path in actual_files}:
        raise ResourceError("resource_manifest.json file set does not match bundle")
    for path in actual_files:
        relative = path.relative_to(bundle).as_posix()
        item = expected_files[relative]
        if path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
            raise ResourceError(f"Manifest hash/size mismatch: {relative}")
    if calculate_bundle_hash(bundle, actual_files) != manifest["bundle_sha256"]:
        raise ResourceError("Bundle SHA-256 mismatch")

    total_size = sum(path.stat().st_size for path in bundle_files(bundle))
    if total_size > policy["limits"]["bundle_bytes"]:
        raise ResourceError(f"Bundle is {total_size} bytes; limit is {policy['limits']['bundle_bytes']}")
    oversized = [
        path for path in bundle_files(bundle) if path.stat().st_size > policy["limits"]["single_file_bytes"]
    ]
    if oversized:
        raise ResourceError("Files exceed device transfer limit: " + ", ".join(path.name for path in oversized))
    if len(bundle_files(bundle)) > policy["limits"]["bundle_files"]:
        raise ResourceError(
            f"Bundle contains {len(bundle_files(bundle))} files; "
            f"device limit is {policy['limits']['bundle_files']}"
        )
    if desktop is not None:
        desktop_catalog = read_json(desktop / "desktop_catalog.json")
        schema_validate(desktop_catalog, "desktop-catalog.schema.json", "desktop_catalog.json")
        if desktop_catalog["version"] != manifest["bundle_version"]:
            raise ResourceError("Desktop catalog version does not match the device bundle")
        if [item["id"] for item in desktop_catalog["expressions"]] != [
            item["id"] for item in catalog["expressions"]
        ]:
            raise ResourceError("Desktop and device catalog ordering/IDs do not match")
        expected_desktop_files = {"desktop_catalog.json"}
        for item in desktop_catalog["expressions"]:
            expected_desktop_files.add(item["preview"])
            expected_desktop_files.update(
                asset["path"] for asset in item.get("creator", {}).values()
            )
        actual_desktop_files = {
            path.relative_to(desktop).as_posix() for path in desktop_files(desktop)
        }
        if actual_desktop_files != expected_desktop_files:
            raise ResourceError("Desktop file set does not match desktop_catalog.json")
        for item in desktop_catalog["expressions"]:
            preview = desktop / item["preview"]
            if not preview.is_file() or sha256_file(preview) != item["preview_sha256"]:
                raise ResourceError(f"Desktop preview hash mismatch: {item['id']}")
            for kind, asset in item.get("creator", {}).items():
                path = desktop / asset["path"]
                if (
                    not path.is_file()
                    or path.stat().st_size != asset["size"]
                    or sha256_file(path) != asset["sha256"]
                ):
                    raise ResourceError(f"Desktop {kind} hash mismatch: {item['id']}")
                if kind == "action":
                    schema_validate(read_json(path), "action.schema.json", str(path))
                elif path.stat().st_size % 2:
                    raise ResourceError(f"Desktop sound is not 16-bit PCM: {item['id']}")
    return {
        "expressions": len(catalog_by_id),
        "dynamic_expressions": device_contract["dynamic_expressions"],
        "device_first_frames": device_contract["first_frames"],
        "actions": sum("action" in item["assets"] for item in catalog_by_id.values()),
        "sounds": sum("sound" in item["assets"] for item in catalog_by_id.values()),
        "frames": total_frames,
        "bytes": total_size,
    }


def validate_packaged_device_archive(archive: Path) -> dict[str, int]:
    """Re-read the final tar.gz and validate the exact bytes sent to the ESP32."""
    with tempfile.TemporaryDirectory(prefix="watche-sd-archive-verify-") as temporary:
        root = Path(temporary)
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            names = [member.name for member in members]
            if any(member.isdir() or member.issym() or member.islnk() or Path(member.name).is_absolute() or ".." in Path(member.name).parts for member in members):
                raise ResourceError("Packaged SD archive contains an unsafe entry")
            if len(names) != len(set(names)):
                raise ResourceError("Packaged SD archive contains duplicate paths")
            for member in members:
                source = tar.extractfile(member)
                if source is None:
                    raise ResourceError(f"Unable to read packaged archive entry: {member.name}")
                target = root / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output)

        manifest = read_json(root / "resource_manifest.json")
        schema_validate(manifest, "resource-manifest.schema.json", "packaged resource_manifest.json")
        catalog = read_json(root / "official_catalog.json")
        schema_validate(catalog, "resource-catalog.schema.json", "packaged official_catalog.json")
        expected = {item["path"]: item for item in manifest["files"]}
        actual = {
            path.relative_to(root).as_posix()
            for path in bundle_files(root, include_manifest=False)
        }
        if set(expected) != actual:
            raise ResourceError("Packaged SD archive file set does not match its manifest")
        for relative, item in expected.items():
            path = root / relative
            if path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
                raise ResourceError(f"Packaged SD archive hash mismatch: {relative}")
        return validate_device_playback_contract(catalog, root, load_policy())


def package_resources(bundle: Path, desktop: Path, dist: Path, version: str) -> dict[str, Any]:
    manifest = read_json(bundle / "resource_manifest.json")
    if manifest.get("bundle_version") != version:
        raise ResourceError(
            f"Bundle version {manifest.get('bundle_version')} does not match requested package {version}"
        )
    desktop_catalog = read_json(desktop / "desktop_catalog.json")
    schema_validate(desktop_catalog, "desktop-catalog.schema.json", "desktop_catalog.json")
    if desktop_catalog.get("version") != version:
        raise ResourceError(
            f"Desktop catalog version {desktop_catalog.get('version')} "
            f"does not match requested package {version}"
        )
    dist.mkdir(parents=True, exist_ok=True)
    version_dir = dist / version
    if version_dir.exists():
        shutil.rmtree(version_dir)
    version_dir.mkdir(parents=True)
    archive = version_dir / f"watche-sd-resources-{version}.tar.gz"
    archive_files = bundle_files(bundle)
    write_deterministic_tar_gz(archive, bundle, archive_files, "watche-sd-")
    transfer_limit = load_policy()["limits"]["transfer_archive_bytes"]
    if archive.stat().st_size > transfer_limit:
        archive.unlink()
        raise ResourceError(
            f"Compressed archive exceeds the device transfer limit of {transfer_limit} bytes"
        )
    archive_contract = validate_packaged_device_archive(archive)
    expanded_size = sum(path.stat().st_size for path in archive_files)
    object_count = sum(
        1
        for path in archive_files
        if path.relative_to(bundle).as_posix().startswith("assets/")
    )
    catalog_source = bundle / "official_catalog.json"
    catalog_target = version_dir / "official_catalog.json"
    shutil.copyfile(catalog_source, catalog_target)
    desktop_catalog_target = version_dir / "desktop_catalog.json"
    shutil.copyfile(desktop / "desktop_catalog.json", desktop_catalog_target)
    desktop_archive_files = desktop_files(desktop)
    desktop_archive = version_dir / f"watche-desktop-resources-{version}.tar.gz"
    write_deterministic_tar_gz(
        desktop_archive,
        desktop,
        desktop_archive_files,
        "watche-desktop-",
    )
    ota = {
        "schema_version": 3,
        "product": "WatcheRobot-S3",
        "version": version,
        "layout_revision": 2,
        "protocol": "WRSD/2",
        "published_at": utc_now(),
        "archive": {
            "name": archive.name,
            "format": "tar.gz",
            "size": archive.stat().st_size,
            "expanded_size": expanded_size,
            "file_count": len(archive_files),
            "object_count": object_count,
            "sha256": sha256_file(archive),
            "github_url": f"{GITHUB_RELEASE_BASE}/{version}/{archive.name}",
            "tos_url": f"{TOS_PUBLIC_BASE}/{version}/{archive.name}",
            "tos_uri": f"tos://erroright/WatcherRobot/sd/{version}/{archive.name}",
        },
        "catalog": {
            "name": "official_catalog.json",
            "github_url": f"{GITHUB_RELEASE_BASE}/{version}/official_catalog.json",
            "tos_url": f"{TOS_PUBLIC_BASE}/{version}/official_catalog.json",
            "tos_uri": f"tos://erroright/WatcherRobot/sd/{version}/official_catalog.json",
            "sha256": sha256_file(catalog_target),
        },
        "desktop": {
            "catalog": {
                "name": desktop_catalog_target.name,
                "size": desktop_catalog_target.stat().st_size,
                "github_url": (
                    f"{GITHUB_RELEASE_BASE}/{version}/{desktop_catalog_target.name}"
                ),
                "tos_url": f"{TOS_PUBLIC_BASE}/{version}/{desktop_catalog_target.name}",
                "tos_uri": (
                    f"tos://erroright/WatcherRobot/sd/{version}/{desktop_catalog_target.name}"
                ),
                "sha256": sha256_file(desktop_catalog_target),
            },
            "archive": {
                "name": desktop_archive.name,
                "format": "tar.gz",
                "size": desktop_archive.stat().st_size,
                "expanded_size": sum(path.stat().st_size for path in desktop_archive_files),
                "file_count": len(desktop_archive_files),
                "sha256": sha256_file(desktop_archive),
                "github_url": f"{GITHUB_RELEASE_BASE}/{version}/{desktop_archive.name}",
                "tos_url": f"{TOS_PUBLIC_BASE}/{version}/{desktop_archive.name}",
                "tos_uri": (
                    f"tos://erroright/WatcherRobot/sd/{version}/{desktop_archive.name}"
                ),
            },
        },
    }
    schema_validate(ota, "ota-manifest.schema.json", "ota-manifest.json")
    write_json(version_dir / "ota-manifest.json", ota)
    write_json(dist / "latest.json", ota)
    print(
        "Validated final device archive: "
        f"{archive_contract['first_frames']} first frame(s), "
        f"{archive_contract['dynamic_expressions']} dynamic expression(s)."
    )
    return ota


def next_version(releases_path: Path) -> str:
    if not releases_path.is_file():
        return "v0.0.1"
    releases = read_json(releases_path)
    schema_validate(releases, "release-index.schema.json", str(releases_path))
    versions = releases.get("versions", []) if isinstance(releases, dict) else []
    parsed = []
    for version in versions:
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", str(version))
        if match:
            parsed.append(tuple(int(part) for part in match.groups()))
    if not parsed:
        return "v0.0.1"
    major, minor, patch = max(parsed)
    return f"v{major}.{minor}.{patch + 1}"


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    subcommands = command.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build")
    build.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    build.add_argument("--pcm-root", type=Path, default=DEFAULT_PCM)
    build.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE)
    build.add_argument("--desktop-root", type=Path, default=DEFAULT_DESKTOP)
    build.add_argument("--version", required=True)
    validate = subcommands.add_parser("validate")
    validate.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    validate.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE)
    validate.add_argument("--desktop-root", type=Path, default=DEFAULT_DESKTOP)
    package = subcommands.add_parser("package")
    package.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE)
    package.add_argument("--desktop-root", type=Path, default=DEFAULT_DESKTOP)
    package.add_argument("--dist-root", type=Path, default=DEFAULT_DIST)
    package.add_argument("--version", required=True)
    next_command = subcommands.add_parser("next-version")
    next_command.add_argument(
        "--releases",
        type=Path,
        default=ROOT / "official" / "releases" / "index.json",
    )
    return command


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "build":
            result = build_resources(
                args.source_root.resolve(),
                args.pcm_root.resolve(),
                args.bundle_root.resolve(),
                args.desktop_root.resolve(),
                args.version,
            )
            print(
                f"Built {result['expressions']} expression(s), {result['actions']} action(s), "
                f"{result['sounds']} sound(s), {result['frames']} frame(s)."
            )
            for resource_id, changes in result["action_adjustments"].items():
                print(f"  normalized action {resource_id}: {len(changes)} angle adjustment(s)")
            print(f"Bundle SHA-256: {result['bundle_sha256']}")
        elif args.command == "validate":
            result = validate_resources(
                args.source_root.resolve(),
                args.bundle_root.resolve(),
                args.desktop_root.resolve(),
            )
            print(
                f"Validated {result['expressions']} expression(s), {result['actions']} action(s), "
                f"{result['sounds']} sound(s), {result['frames']} frame(s), {result['bytes']} bytes."
            )
        elif args.command == "package":
            result = package_resources(
                args.bundle_root.resolve(),
                args.desktop_root.resolve(),
                args.dist_root.resolve(),
                args.version,
            )
            print(f"Packaged {args.version}: {result['archive']['sha256']}")
        else:
            print(next_version(args.releases.resolve()))
    except (OSError, ResourceError) as exc:
        print(f"Resource pipeline failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
