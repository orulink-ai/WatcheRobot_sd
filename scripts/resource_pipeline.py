#!/usr/bin/env python3
"""Build, validate and package WatcheRobot SD resources (Layout Revision 3).

SD-card root layout:
  /sdcard/
    anim/<id>.animpack
    actions/<id>.json
    sfx/<id>.pcm
    catalog.json
    fixed_states.json
    resource_manifest.json

Mobile App artifacts:
  mobile_catalog.json (schema_version=1, with localized display_names)
  mobile/gif/<id>.gif
"""

from __future__ import annotations

import argparse
import copy
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
DEFAULT_MOBILE = ROOT / "official" / "mobile"
DEFAULT_DIST = ROOT / "dist"
GITHUB_RELEASE_BASE = "https://github.com/orulink-ai/WatcheRobot_sd/releases/download"
CN_PUBLIC_BASE = "https://download.orulink.cn/WatcherRobot/sd"

PACK_MAGIC = b"ANPK"
PACK_VERSION_EFC = 3
PACK_HEADER_FMT = "<4sHHHHBBHIII"
FRAME_DESC_FMT = "<IIHH"
FRAME_FLAG_EFC = 0x0002
FRAME_FLAG_EFC_SWAPPED = 0x0004
FRAME_FLAG_EFC_MASK = FRAME_FLAG_EFC | FRAME_FLAG_EFC_SWAPPED
EFC_FLAG_PREDICTOR = 0x08
EFC_MAX_SEGMENT_BYTES = 2 * 1024 * 1024
ANIMATION_FORMAT = "animpack-v3"
DEVICE_CATALOG_MAX_BYTES = 128 * 1024
DEVICE_PATH_MAX_BYTES = 192
DEVICE_DISPLAY_NAME_MAX_BYTES = 192
LAYOUT_REVISION = 3

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

ASSET_DIRECTORIES = {
    "anim": "anim",
    "action": "actions",
    "sfx": "sfx",
}

ASSET_EXTENSIONS = {
    "anim": ".animpack",
    "action": ".json",
    "sfx": ".pcm",
}

ASSET_FORMATS = {
    "anim": ANIMATION_FORMAT,
    "action": "firmware-action-json-v1",
    "sfx": "pcm-s16le-24khz-mono",
}


class ResourceError(ValueError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResourceError(f"Invalid JSON {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_relative_path(kind: str, resource_id: str) -> str:
    if kind not in ASSET_DIRECTORIES:
        raise ResourceError(f"Unsupported asset kind: {kind}")
    return f"{ASSET_DIRECTORIES[kind]}/{resource_id}{ASSET_EXTENSIONS[kind]}"


def store_asset_file(
    bundle: Path,
    source: Path,
    kind: str,
    resource_id: str,
) -> dict[str, Any]:
    relative = asset_relative_path(kind, resource_id)
    target = bundle / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return {
        "kind": kind,
        "path": relative,
        "sha256": sha256_file(target),
        "size": target.stat().st_size,
        "format": ASSET_FORMATS[kind],
    }


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


def write_animpack_efc(
    target: Path,
    frames: list[tuple[Image.Image, int]],
    fps: int,
) -> dict[str, Any]:
    """Encode RGB565 frames into segmented single-stream EFC (animpack-v3).

    Hard guarantees enforced before writing:
      - Exact big-endian (LV_COLOR_16_SWAP=1) pixel layout matching firmware LCD.
      - Single-stream legacy EFC only ((blob[5] & 0x08) == 0) to bound ESP32-S3 PSRAM.
      - Adaptive 2-slot prefetch budget segmentation with loop wrap-around protection.
      - Per-segment byte cap (<= EFC_MAX_SEGMENT_BYTES) and 100% pixel-exact roundtrip check.
    """
    import numpy as np
    import efc_codec
    from anim_efc_segmenter import (
        SegmentBudgetConfig,
        pack_segmented_animpack_v3,
        plan_adaptive_efc_segments,
    )

    width, height = frames[0][0].size
    delays_ms = [max(20, int(delay)) for _, delay in frames]
    raw_frames = [rgba_to_rgb565(frame) for frame, _ in frames]
    # rgba_to_rgb565 emits big-endian RGB565 bytes; viewing as <u2 yields the exact
    # byte-swapped u16 words expected by ANIM_FRAME_FLAG_EFC_SWAPPED on ESP32-S3.
    swapped = np.empty((len(raw_frames), height, width), dtype="<u2")
    for idx, raw in enumerate(raw_frames):
        swapped[idx] = np.frombuffer(raw, dtype="<u2").reshape((height, width))

    def encode_span(start: int, count: int) -> bytes:
        blob = efc_codec.encode_frames(
            swapped[start : start + count],
            tile=4,
            compress=True,
            legacy=True,
            hold=False,
        )
        if len(blob) < 20 or blob[:4] != b"EFC1":
            raise ResourceError(f"EFC encoder emitted invalid header for {target.name}")
        if (blob[5] & EFC_FLAG_PREDICTOR) != 0:
            raise ResourceError(
                f"EFC encoder emitted unsupported predictor-plane stream (flags=0x{blob[5]:02x}) for {target.name}"
            )
        return blob

    # Always plan with loop=True so multi-segment clips have wrap-around prefetch safety
    # even when firmware plays an expression in looping behavior states.
    seg_res = plan_adaptive_efc_segments(
        delays_ms,
        encode_span,
        loop=True,
        config=SegmentBudgetConfig(),
    )

    reconstructed = []
    for seg in seg_res.segments:
        if len(seg.payload) > EFC_MAX_SEGMENT_BYTES:
            raise ResourceError(
                f"EFC segment for {target.name} (frames {seg.start_frame}..{seg.start_frame + seg.frame_count - 1}) "
                f"exceeds {EFC_MAX_SEGMENT_BYTES} bytes ({len(seg.payload)})"
            )
        dec, _meta = efc_codec.decode_frames(seg.payload)
        if dec.shape != (seg.frame_count, height, width):
            raise ResourceError(f"EFC roundtrip geometry mismatch for {target.name}")
        reconstructed.append(dec)
    full_rt = np.concatenate(reconstructed, axis=0)
    if not np.array_equal(full_rt, swapped):
        raise ResourceError(f"EFC pixel-exact lossless roundtrip check failed for {target.name}")

    v3_bytes = pack_segmented_animpack_v3(
        width=width,
        height=height,
        loop=False,
        default_delay_ms=max(1, round(1000 / fps)),
        frame_delays_ms=delays_ms,
        segments=seg_res.segments,
        swapped_rgb565=True,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(v3_bytes)
    return {
        "width": width,
        "height": height,
        "frames": len(frames),
        "raw_bytes": len(raw_frames) * width * height * 2,
        "encoded_bytes": len(v3_bytes),
        "segments": seg_res.segment_count,
        "first_segment_bytes": seg_res.first_segment_bytes,
        "max_segment_bytes": seg_res.max_segment_bytes,
        "prefetch_stall_free": bool(seg_res.single_pass_report.stall_free and seg_res.loop_report.stall_free),
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
    if magic != PACK_MAGIC or version != PACK_VERSION_EFC:
        raise ResourceError(f"Unsupported AnimPack header: {path}")
    if width <= 0 or height <= 0 or frame_count <= 0 or raw_size != width * height * 2:
        raise ResourceError(f"Invalid AnimPack dimensions/count: {path}")
    if toc_offset != header_size or payload_offset != header_size + frame_count * descriptor_size:
        raise ResourceError(f"Invalid AnimPack offsets: {path}")

    import numpy as np
    import efc_codec

    payload_total = len(data) - payload_offset
    if payload_total <= 0:
        raise ResourceError(f"Empty EFC AnimPack payload: {path}")
    descs: list[tuple[int, int, int, int]] = []
    for index in range(frame_count):
        frame_offset, frame_size, frame_delay, flags = struct.unpack_from(
            FRAME_DESC_FMT, data, toc_offset + index * descriptor_size
        )
        if (
            frame_delay <= 0
            or (flags & FRAME_FLAG_EFC) == 0
            or (flags & ~FRAME_FLAG_EFC_MASK) != 0
        ):
            raise ResourceError(f"Invalid EFC AnimPack frame {index}: {path}")
        if frame_offset == 0 and frame_size == 0:
            frame_size = payload_total
        if (
            frame_size <= 0
            or frame_size > EFC_MAX_SEGMENT_BYTES
            or frame_offset > payload_total
            or frame_size > payload_total - frame_offset
        ):
            raise ResourceError(f"Out-of-bounds EFC segment at frame {index}: {path}")
        descs.append((frame_offset, frame_size, frame_delay, flags))

    frames: list[bytes] = []
    cursor = 0
    while cursor < frame_count:
        seg_off, seg_sz, _seg_delay, seg_flags = descs[cursor]
        seg_end = cursor + 1
        while seg_end < frame_count and descs[seg_end][0] == seg_off and descs[seg_end][1] == seg_sz:
            if descs[seg_end][3] != seg_flags:
                raise ResourceError(f"Inconsistent EFC flags inside segment at frame {seg_end}: {path}")
            seg_end += 1
        expected_seg_frames = seg_end - cursor
        seg_blob = data[payload_offset + seg_off : payload_offset + seg_off + seg_sz]
        if len(seg_blob) < 20 or seg_blob[:4] != b"EFC1":
            raise ResourceError(f"Invalid EFC1 segment magic at frame {cursor}: {path}")
        if (seg_blob[5] & EFC_FLAG_PREDICTOR) != 0:
            raise ResourceError(
                f"Unsupported predictor-plane EFC stream (flags=0x{seg_blob[5]:02x}) at frame {cursor}: {path}"
            )
        decoded, _meta = efc_codec.decode_frames(seg_blob)
        if decoded.shape != (expected_seg_frames, height, width):
            raise ResourceError(
                f"EFC segment frame/geometry mismatch at frame {cursor}: "
                f"got {decoded.shape}, expected {(expected_seg_frames, height, width)} ({path})"
            )
        if (seg_flags & FRAME_FLAG_EFC_SWAPPED) == 0:
            decoded = decoded.byteswap()
        decoded_le = np.ascontiguousarray(decoded, dtype="<u2")
        for local_idx in range(expected_seg_frames):
            frames.append(decoded_le[local_idx].tobytes())
        cursor = seg_end

    return {
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "loop": loop,
        "delay": delay,
        "version": version,
    }, frames


def device_asset_path(asset: dict[str, Any]) -> str:
    """Return the exact SD-card path resolved by ESP32 firmware (Layout Revision 3)."""
    return f"/sdcard/{asset['path']}"


def validate_device_playback_contract(
    catalog: dict[str, Any], bundle: Path, policy: dict[str, Any]
) -> dict[str, int]:
    """Validate every expression using the ESP32 root-layout SD contract."""
    expressions = catalog["expressions"]
    if (bundle / "catalog.json").stat().st_size > DEVICE_CATALOG_MAX_BYTES:
        raise ResourceError("catalog.json exceeds the ESP32 catalog size limit")

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

        for asset_key, asset in entry["assets"].items():
            expected_kind = {"animation": "anim", "action": "action", "sound": "sfx"}[asset_key]
            expected_relative = asset_relative_path(expected_kind, resource_id)
            if asset["kind"] != expected_kind or asset["path"] != expected_relative:
                raise ResourceError(f"Unexpected {asset_key} reference for {resource_id}")
            resolved = device_asset_path(asset)
            if len(resolved.encode("utf-8")) >= DEVICE_PATH_MAX_BYTES:
                raise ResourceError(f"ESP32 asset path is too long: {resource_id}/{asset_key}")
            path = bundle / expected_relative
            if not path.is_file() or path.stat().st_size != asset["size"] or sha256_file(path) != asset["sha256"]:
                raise ResourceError(f"ESP32 asset resolution failed: {resource_id}/{asset_key}")

        animation_path = bundle / entry["assets"]["animation"]["path"]
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


def build_resources(
    source: Path,
    pcm_root: Path,
    bundle: Path,
    mobile: Path,
    version: str,
) -> dict[str, Any]:
    policy = load_policy()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise ResourceError(f"Invalid resource version: {version}")
    snapshot = source_snapshot(source)
    records = sorted(snapshot["records"], key=lambda item: item["order"])
    if bundle.exists():
        shutil.rmtree(bundle)
    if mobile.exists():
        shutil.rmtree(mobile)
    bundle.mkdir(parents=True)
    mobile.mkdir(parents=True)
    staging = bundle / ".build"
    staging.mkdir()

    catalog: list[dict[str, Any]] = []
    adjustments: dict[str, list[str]] = {}
    fps = policy["display"]["default_fps"]
    raw_anim_bytes = 0
    encoded_anim_bytes = 0
    total_segments = 0
    prefetch_stall_free_all = True
    for record in records:
        resource_id = record["resource_id"]
        gif_path = source / "gif" / f"{resource_id}.gif"
        if not gif_path.is_file():
            raise ResourceError(f"Source GIF is missing for {resource_id}")
        frames = read_gif(gif_path, policy)
        animation_temp = staging / f"{resource_id}.animpack"
        stats = write_animpack_efc(animation_temp, frames, fps)
        raw_anim_bytes += int(stats["raw_bytes"])
        encoded_anim_bytes += int(stats["encoded_bytes"])
        total_segments += int(stats["segments"])
        prefetch_stall_free_all = prefetch_stall_free_all and bool(stats["prefetch_stall_free"])
        assets: dict[str, Any] = {
            "animation": store_asset_file(bundle, animation_temp, "anim", resource_id)
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
            assets["action"] = store_asset_file(bundle, action_temp, "action", resource_id)
            if changed:
                adjustments[resource_id] = changed
        sound_path = pcm_root / f"{resource_id}.pcm"
        if record.get("sound") is not None:
            if not sound_path.is_file():
                raise ResourceError(f"PCM conversion output is missing for {resource_id}")
            assets["sound"] = store_asset_file(bundle, sound_path, "sfx", resource_id)
        catalog.append(entry)

    catalog_document = {
        "schema_version": 3,
        "format": "watche-sd-catalog",
        "version": version,
        "expressions": catalog,
    }
    schema_validate(catalog_document, "resource-catalog.schema.json", "catalog.json")
    write_json(bundle / "catalog.json", catalog_document)

    fixed_states = {
        "schema_version": 1,
        "states": {state: state for state in policy["fixed_states"]},
    }
    schema_validate(fixed_states, "fixed-states.schema.json", "fixed_states.json")
    write_json(bundle / "fixed_states.json", fixed_states)

    mobile_entries = []
    mobile_digest = hashlib.sha256()
    for record, entry in zip(records, catalog):
        relative = f"gif/{entry['id']}.gif"
        source_gif = source / relative
        target_gif = mobile / relative
        target_gif.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_gif, target_gif)
        mobile_device = {"image_name": entry["id"]}
        if "action" in entry["assets"]:
            mobile_device["action_file"] = entry["id"]
        if "sound" in entry["assets"]:
            mobile_device["sound_file"] = entry["id"]
        mobile_entry: dict[str, Any] = {
            "id": entry["id"],
            "display_name": record["display_name"],
            "order": entry["order"],
            "preview": relative,
            "preview_sha256": sha256_file(target_gif),
            "preview_size": target_gif.stat().st_size,
            "device": mobile_device,
        }
        if "display_names" in record:
            mobile_entry["display_names"] = record["display_names"]
        mobile_entries.append(mobile_entry)
        mobile_digest.update(
            json.dumps(
                mobile_entry,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    mobile_catalog = {
        "schema_version": 1,
        "format": "watche-mobile-expression-catalog",
        "version": version,
        "content_hash": mobile_digest.hexdigest(),
        "expressions": mobile_entries,
    }
    schema_validate(mobile_catalog, "mobile-catalog.schema.json", "mobile_catalog.json")
    write_json(mobile / "mobile_catalog.json", mobile_catalog)

    shutil.rmtree(staging)
    manifest = write_resource_manifest(bundle, version, policy, catalog, snapshot["imported_at"])
    validation = validate_resources(source, bundle, mobile)
    return {
        "version": version,
        "animation_format": ANIMATION_FORMAT,
        "expressions": len(catalog),
        "actions": sum("action" in item["assets"] for item in catalog),
        "sounds": sum("sound" in item["assets"] for item in catalog),
        "frames": validation["frames"],
        "raw_anim_bytes": raw_anim_bytes,
        "encoded_anim_bytes": encoded_anim_bytes,
        "total_segments": total_segments,
        "prefetch_stall_free_all": prefetch_stall_free_all,
        "bundle_sha256": manifest["bundle_sha256"],
        "action_adjustments": adjustments,
    }


def bundle_files(bundle: Path, include_manifest: bool = True) -> list[Path]:
    files = [path for path in bundle.rglob("*") if path.is_file()]
    if not include_manifest:
        files = [path for path in files if path.name != "resource_manifest.json"]
    return sorted(files, key=lambda path: path.relative_to(bundle).as_posix())


def mobile_files(mobile: Path) -> list[Path]:
    return sorted(
        (path for path in mobile.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(mobile).as_posix(),
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
                info.mode = 0o644
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
    timestamp: str,
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
        "schema_version": 3,
        "product": policy["product"],
        "bundle_id": "adepto-official",
        "bundle_version": version,
        "layout_revision": LAYOUT_REVISION,
        "generated_at": timestamp,
        "compatibility": {
            "layout": "/",
            "animation": {
                "format": ANIMATION_FORMAT,
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
                "anim_root": "anim",
                "actions_root": "actions",
                "sfx_root": "sfx",
                "format": ANIMATION_FORMAT,
                "object_count": sum(len(entry["assets"]) for entry in catalog),
            },
            "catalog": {
                "path": "catalog.json",
                "format": "watche-sd-catalog-v3",
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


def validate_resources(
    source: Path,
    bundle: Path,
    mobile: Path | None = None,
) -> dict[str, int]:
    policy = load_policy()
    snapshot = source_snapshot(source)
    catalog = read_json(bundle / "catalog.json")
    schema_validate(catalog, "resource-catalog.schema.json", "catalog.json")
    manifest = read_json(bundle / "resource_manifest.json")
    schema_validate(manifest, "resource-manifest.schema.json", "resource_manifest.json")
    fixed_states = read_json(bundle / "fixed_states.json")
    schema_validate(fixed_states, "fixed-states.schema.json", "fixed_states.json")
    if set(fixed_states["states"]) != set(policy["fixed_states"]):
        raise ResourceError("fixed_states.json must contain exactly the eight fixed states")
    if catalog["version"] != manifest["bundle_version"]:
        raise ResourceError("catalog.json version does not match resource_manifest.json")

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
        animation_path = bundle / animation["path"]
        if animation_path.stat().st_size != animation["size"]:
            raise ResourceError(f"Animation size mismatch for {resource_id}")
        _header, frames = decode_animpack(animation_path)
        if len(frames) != len(source_frames):
            raise ResourceError(f"Frame count mismatch for {resource_id}")
        for index, ((image, _), actual) in enumerate(zip(source_frames, frames)):
            if actual != rgba_to_rgb565(image):
                raise ResourceError(f"RGB565 pixel/byte-order mismatch: {resource_id} frame {index}")
        total_frames += len(frames)
        if "action" in entry["assets"]:
            reference = entry["assets"]["action"]
            action_path = bundle / reference["path"]
            action = read_json(action_path)
            schema_validate(action, "action.schema.json", str(action_path))
        if "sound" in entry["assets"]:
            reference = entry["assets"]["sound"]
            sound = bundle / reference["path"]
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

    if mobile is not None:
        mobile_catalog = read_json(mobile / "mobile_catalog.json")
        schema_validate(mobile_catalog, "mobile-catalog.schema.json", "mobile_catalog.json")
        if mobile_catalog["version"] != manifest["bundle_version"]:
            raise ResourceError("Mobile catalog version does not match the device bundle")
        if [item["id"] for item in mobile_catalog["expressions"]] != [
            item["id"] for item in catalog["expressions"]
        ]:
            raise ResourceError("Mobile and device catalog ordering/IDs do not match")
        expected_mobile_files = {"mobile_catalog.json"}
        expected_mobile_files.update(item["preview"] for item in mobile_catalog["expressions"])
        actual_mobile_files = {
            path.relative_to(mobile).as_posix() for path in mobile_files(mobile)
        }
        if actual_mobile_files != expected_mobile_files:
            raise ResourceError("Mobile file set does not match mobile_catalog.json")
        for item in mobile_catalog["expressions"]:
            preview = mobile / item["preview"]
            if (
                not preview.is_file()
                or preview.stat().st_size != item["preview_size"]
                or sha256_file(preview) != item["preview_sha256"]
            ):
                raise ResourceError(f"Mobile preview hash mismatch: {item['id']}")

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
    """Re-read the final tar.gz and validate the exact bytes written to SD."""
    with tempfile.TemporaryDirectory(prefix="watche-sd-archive-verify-") as temporary:
        root = Path(temporary)
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            names = [member.name for member in members]
            if any(
                member.isdir()
                or member.issym()
                or member.islnk()
                or Path(member.name).is_absolute()
                or ".." in Path(member.name).parts
                for member in members
            ):
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
        catalog = read_json(root / "catalog.json")
        schema_validate(catalog, "resource-catalog.schema.json", "packaged catalog.json")
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


def version_tuple(version: str) -> tuple[int, ...]:
    if not re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ResourceError(f"Invalid release version: {version}")
    return tuple(map(int, version[1:].split(".")))


def regional_manifest(source: dict[str, Any], region: str) -> dict[str, Any]:
    ota = copy.deepcopy(source)
    version = ota["version"]
    version_tuple(version)
    ota["region"] = region
    base = CN_PUBLIC_BASE if region == "cn" else GITHUB_RELEASE_BASE
    for asset in [ota["archive"], ota["catalog"], ota["mobile"]["catalog"]]:
        for key in ("tos_url", "tos_uri", "github_url"):
            asset.pop(key, None)
        asset["url"] = f"{base}/{version}/{asset['name']}"
        if region == "global":
            asset["github_url"] = asset["url"]
    gifs = ota["mobile"]["assets"]
    for key in ("tos_base_url", "tos_uri", "github_base_url"):
        gifs.pop(key, None)
    gifs["base_url"] = (f"{base}/{version}/mobile/gif/" if region == "cn" else
                        f"https://raw.githubusercontent.com/orulink-ai/WatcheRobot_sd/{version}/official/mobile/gif/")
    if region == "global":
        gifs["github_base_url"] = gifs["base_url"]
    schema_validate(ota, "ota-manifest.schema.json", "ota-manifest.json")
    return ota


def verify_assets(directory: Path, ota: dict[str, Any]) -> dict[str, Any]:
    for asset in [ota["archive"], ota["catalog"], ota["mobile"]["catalog"]]:
        path = directory / asset["name"]
        if sha256_file(path) != asset["sha256"] or path.stat().st_size != asset.get("size", path.stat().st_size):
            raise ResourceError(f"Release asset differs from manifest: {path.name}")
    catalog = read_json(directory / "mobile_catalog.json")
    schema_validate(catalog, "mobile-catalog.schema.json", "mobile_catalog.json")
    if catalog["version"] != ota["version"]:
        raise ResourceError("Mobile catalog version differs from the release")
    total = 0
    for item in catalog["expressions"]:
        path = directory / "mobile" / "gif" / (item["id"] + ".gif")
        if (item["preview"] != "gif/" + path.name or path.stat().st_size != item["preview_size"]
                or sha256_file(path) != item["preview_sha256"]):
            raise ResourceError(f"GIF differs from mobile catalog: {path.name}")
        total += item["preview_size"]
    assets = ota["mobile"]["assets"]
    if total != assets["size"] or len(catalog["expressions"]) != assets["count"]:
        raise ResourceError("GIF count or total size differs from the release")
    return catalog


def stage_cn_release(directory: Path, ota: dict[str, Any], root: Path, index: Path | None) -> None:
    version = ota["version"]
    if version_tuple(version) < (0, 1, 0):
        raise ResourceError("CN SD distribution starts at v0.1.0")
    manifest = regional_manifest(ota, "cn")
    catalog = verify_assets(directory, manifest)
    history = read_json(index)["releases"] if index else []
    releases = {entry["version"]: entry for entry in history if version_tuple(entry["version"]) >= (0, 1, 0)}
    if any(version_tuple(v) > version_tuple(version) for v in releases):
        raise ResourceError("Refusing to roll back latest.json")
    releases[version] = {"version": version, "published_at": ota["published_at"]}
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as work:
        temp = Path(work)
        staged = temp / version
        staged.mkdir()
        for asset in (manifest["archive"], manifest["catalog"], manifest["mobile"]["catalog"]):
            shutil.copyfile(directory / asset["name"], staged / asset["name"])
        for item in catalog["expressions"]:
            path = Path("mobile/gif") / (item["id"] + ".gif")
            (staged / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(directory / path, staged / path)
        verify_assets(staged, manifest)
        write_json(staged / "ota-manifest.json", manifest)
        write_json(temp / "releases.json", {"releases": [releases[v] for v in sorted(releases, key=version_tuple, reverse=True)]})
        write_json(temp / "latest.json", manifest)
        dest = root / version
        if dest.exists():
            dest.rename(temp / "previous")
        try:
            staged.rename(dest)
        except OSError:
            if (temp / "previous").exists():
                (temp / "previous").rename(dest)
            raise
        for name in ("releases.json", "latest.json"):
            (temp / name).replace(root / name)


def package_resources(
    bundle: Path,
    mobile: Path,
    dist: Path,
    version: str,
    cn_index: Path | None = None,
) -> dict[str, Any]:
    version_tuple(version)
    manifest = read_json(bundle / "resource_manifest.json")
    if manifest.get("bundle_version") != version:
        raise ResourceError(
            f"Bundle version {manifest.get('bundle_version')} does not match requested package {version}"
        )
    mobile_catalog = read_json(mobile / "mobile_catalog.json")
    schema_validate(mobile_catalog, "mobile-catalog.schema.json", "mobile_catalog.json")
    if mobile_catalog.get("version") != version:
        raise ResourceError(
            f"Mobile catalog version {mobile_catalog.get('version')} "
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
        if path.relative_to(bundle).as_posix().split("/", 1)[0] in {"anim", "actions", "sfx"}
    )
    catalog_source = bundle / "catalog.json"
    catalog_target = version_dir / "catalog.json"
    shutil.copyfile(catalog_source, catalog_target)

    mobile_catalog_target = version_dir / "mobile_catalog.json"
    shutil.copyfile(mobile / "mobile_catalog.json", mobile_catalog_target)
    mobile_asset_root = version_dir / "mobile" / "gif"
    mobile_asset_root.mkdir(parents=True)
    for expression in mobile_catalog["expressions"]:
        shutil.copyfile(
            mobile / expression["preview"],
            mobile_asset_root / f"{expression['id']}.gif",
        )
    mobile_size = sum(expression["preview_size"] for expression in mobile_catalog["expressions"])

    ota = {
        "schema_version": 4,
        "product": "WatcheRobot-S3",
        "version": version,
        "layout_revision": LAYOUT_REVISION,
        "published_at": manifest["generated_at"],
        "archive": {
            "name": archive.name,
            "format": "tar.gz",
            "size": archive.stat().st_size,
            "expanded_size": expanded_size,
            "file_count": len(archive_files),
            "object_count": object_count,
            "sha256": sha256_file(archive),
        },
        "catalog": {
            "name": "catalog.json",
            "sha256": sha256_file(catalog_target),
        },
        "mobile": {
            "catalog": {
                "name": mobile_catalog_target.name,
                "size": mobile_catalog_target.stat().st_size,
                "sha256": sha256_file(mobile_catalog_target),
            },
            "assets": {
                "format": "individual-gif",
                "count": len(mobile_catalog["expressions"]),
                "size": mobile_size,
            },
        },
    }
    ota = regional_manifest(ota, "global")
    if version_tuple(version) >= (0, 1, 0):
        stage_cn_release(version_dir, ota, dist / "cn" / "WatcherRobot" / "sd", cn_index)
    else:
        verify_assets(version_dir, ota)
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
    build.add_argument("--mobile-root", type=Path, default=DEFAULT_MOBILE)
    build.add_argument("--version", required=True)

    validate = subcommands.add_parser("validate")
    validate.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    validate.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE)
    validate.add_argument("--mobile-root", type=Path, default=DEFAULT_MOBILE)

    package = subcommands.add_parser("package")
    package.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE)
    package.add_argument("--mobile-root", type=Path, default=DEFAULT_MOBILE)
    package.add_argument("--dist-root", type=Path, default=DEFAULT_DIST)
    package.add_argument("--version", required=True)
    package.add_argument("--cn-index", type=Path, help="Existing CN releases.json to merge")

    stage = subcommands.add_parser("stage-cn", help="Resume CN staging from verified packaged files")
    stage.add_argument("--version", required=True)
    stage.add_argument("--dist-root", type=Path, default=DEFAULT_DIST)
    stage.add_argument("--cn-index", type=Path, required=True)

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
                args.mobile_root.resolve(),
                args.version,
            )
            raw_mb = result["raw_anim_bytes"] / (1024 * 1024)
            enc_mb = result["encoded_anim_bytes"] / (1024 * 1024)
            ratio = (1.0 - (result["encoded_anim_bytes"] / max(1, result["raw_anim_bytes"]))) * 100.0
            print(
                f"Built {result['expressions']} expression(s), {result['actions']} action(s), "
                f"{result['sounds']} sound(s), {result['frames']} frame(s) "
                f"[{result['animation_format']}: {raw_mb:.2f} MiB -> {enc_mb:.2f} MiB (-{ratio:.1f}%), "
                f"segments={result['total_segments']}, stall_free={result['prefetch_stall_free_all']}]."
            )
            for resource_id, changes in result["action_adjustments"].items():
                print(f"  normalized action {resource_id}: {len(changes)} angle adjustment(s)")
            print(f"Bundle SHA-256: {result['bundle_sha256']}")
        elif args.command == "validate":
            result = validate_resources(
                args.source_root.resolve(),
                args.bundle_root.resolve(),
                args.mobile_root.resolve(),
            )
            print(
                f"Validated {result['expressions']} expression(s), {result['actions']} action(s), "
                f"{result['sounds']} sound(s), {result['frames']} frame(s), {result['bytes']} bytes."
            )
        elif args.command == "package":
            result = package_resources(
                args.bundle_root.resolve(),
                args.mobile_root.resolve(),
                args.dist_root.resolve(),
                args.version,
                args.cn_index,
            )
            print(f"Packaged {args.version}: {result['archive']['sha256']}")
        elif args.command == "stage-cn":
            version_tuple(args.version)
            directory = args.dist_root / args.version
            ota = read_json(directory / "ota-manifest.json")
            if ota.get("version") != args.version:
                raise ResourceError("Packaged manifest version does not match the requested version")
            if ota.get("region") != "global":
                raise ResourceError("Expected the GLOBAL package as the source of CN staging")
            schema_validate(ota, "ota-manifest.schema.json", "ota-manifest.json")
            stage_cn_release(directory, ota, args.dist_root / "cn" / "WatcherRobot" / "sd", args.cn_index)
        else:
            print(next_version(args.releases.resolve()))
    except (OSError, ResourceError) as exc:
        print(f"Resource pipeline failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
