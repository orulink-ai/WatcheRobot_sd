from __future__ import annotations

import importlib.util
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "resource_pipeline.py"
SPEC = importlib.util.spec_from_file_location("resource_pipeline", SCRIPT)
PIPELINE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PIPELINE)


class ResourcePipelineTests(unittest.TestCase):
    fixed_states = [
        "boot",
        "standby",
        "listening",
        "thinking",
        "speaking",
        "processing",
        "error",
        "upgrade",
    ]

    def create_source(self, root: Path, size: tuple[int, int] = (206, 206)) -> Path:
        source = root / "source"
        (source / "gif").mkdir(parents=True)
        (source / "actions").mkdir()
        (source / "sound").mkdir()
        records = []
        for order, resource_id in enumerate(self.fixed_states):
            Image.new("RGB", size, (order * 20, 200, 30)).save(
                source / "gif" / f"{resource_id}.gif",
                save_all=True,
                duration=100,
                loop=0,
            )
            records.append(
                {
                    "source_record_id": f"rec-{resource_id}",
                    "source_label": f"watcher-{resource_id}",
                    "display_name": resource_id,
                    "display_names": {
                        "zh-Hans": resource_id,
                        "en": resource_id.title(),
                        "ja": resource_id,
                    },
                    "resource_id": resource_id,
                    "order": order,
                    "gif": f"gif/{resource_id}.gif",
                    "action": None,
                    "sound": None,
                }
            )
        future_order = len(records)
        future_id = "future_expression"
        Image.new("RGB", size, (30, 90, 220)).save(
            source / "gif" / f"{future_id}.gif",
            save_all=True,
            duration=100,
            loop=0,
        )
        records.append(
            {
                "source_record_id": "rec-future-expression",
                "source_label": "watcher-future-expression",
                "display_name": "future expression",
                "display_names": {
                    "zh-Hans": "未来表情",
                    "en": "Future Expression",
                    "ja": "未来の表情",
                },
                "resource_id": future_id,
                "order": future_order,
                "gif": f"gif/{future_id}.gif",
                "action": None,
                "sound": None,
            }
        )
        action = {
            "scene_name": "Scene",
            "frame_start": 0,
            "frame_end": 10,
            "fps": 10,
            "animated_objects": [
                {
                    "keyframe_data": [
                        {"frame_number": 0, "active_axis": "x", "rotation_angle": 90},
                        {"frame_number": 10, "active_axis": "z", "rotation_angle": 0},
                    ]
                }
            ],
        }
        (source / "actions" / "boot.json").write_text(json.dumps(action), encoding="utf-8")
        (source / "sound" / "boot.pcm").write_bytes(b"\x00\x00\x01\x00")
        records[0]["action"] = "actions/boot.json"
        records[0]["sound"] = "sound/boot.mp3"
        (source / "feishu-snapshot.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "Watcher expression overview",
                    "imported_at": "2026-11-26T00:00:00Z",
                    "records": records,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return source

    def test_build_validate_and_package_flat_root_bundle_without_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_source(root)
            bundle = root / "bundle"
            mobile = root / "mobile"
            result = PIPELINE.build_resources(source, source / "sound", bundle, mobile, "v0.1.1")
            self.assertEqual(9, result["expressions"])
            catalog = json.loads((bundle / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(3, catalog["schema_version"])
            self.assertEqual("watche-sd-catalog", catalog["format"])
            self.assertEqual("v0.1.1", catalog["version"])
            boot = next(item for item in catalog["expressions"] if item["id"] == "boot")
            self.assertEqual("anim/boot.animpack", boot["assets"]["animation"]["path"])
            self.assertEqual("actions/boot.json", boot["assets"]["action"]["path"])
            self.assertEqual("sfx/boot.pcm", boot["assets"]["sound"]["path"])
            boot_pack = bundle / "anim" / "boot.animpack"
            self.assertTrue(boot_pack.is_file())
            self.assertTrue((bundle / "actions" / "boot.json").is_file())
            self.assertTrue((bundle / "sfx" / "boot.pcm").is_file())
            self.assertFalse((bundle / "assets").exists())
            self.assertFalse((bundle / "official_catalog.json").exists())
            boot_header, _ = PIPELINE.decode_animpack(boot_pack)
            self.assertEqual(0, boot_header["loop"])
            self.assertEqual(3, boot_header["version"])
            self.assertEqual("animpack-v3", boot["assets"]["animation"]["format"])
            self.assertEqual(
                set(self.fixed_states),
                set(json.loads((bundle / "fixed_states.json").read_text(encoding="utf-8"))["states"]),
            )
            manifest = json.loads((bundle / "resource_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(3, manifest["schema_version"])
            self.assertEqual(3, manifest["layout_revision"])
            self.assertEqual("v0.1.1", manifest["bundle_version"])
            self.assertEqual("/", manifest["compatibility"]["layout"])
            self.assertEqual("animpack-v3", manifest["compatibility"]["animation"]["format"])
            self.assertEqual("animpack-v3", manifest["contents"]["assets"]["format"])

            validated = PIPELINE.validate_resources(source, bundle, mobile)
            self.assertEqual(9, validated["expressions"])
            self.assertEqual(1, validated["dynamic_expressions"])
            self.assertEqual(9, validated["device_first_frames"])

            mobile_catalog = json.loads((mobile / "mobile_catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(1, mobile_catalog["schema_version"])
            self.assertEqual("watche-mobile-expression-catalog", mobile_catalog["format"])
            self.assertEqual("v0.1.1", mobile_catalog["version"])
            mobile_future = next(
                item for item in mobile_catalog["expressions"] if item["id"] == "future_expression"
            )
            self.assertEqual(
                {
                    "zh-Hans": "未来表情",
                    "en": "Future Expression",
                    "ja": "未来の表情",
                },
                mobile_future["display_names"],
            )
            self.assertTrue((mobile / "gif" / "boot.gif").is_file())

            ota = PIPELINE.package_resources(bundle, mobile, root / "dist", "v0.1.1")
            self.assertEqual(4, ota["schema_version"])
            self.assertEqual(3, ota["layout_revision"])
            self.assertNotIn("desktop", ota)
            self.assertNotIn("protocol", ota)
            self.assertEqual("catalog.json", ota["catalog"]["name"])
            self.assertEqual("watche-sd-resources-v0.1.1.tar.gz", ota["archive"]["name"])
            self.assertEqual(11, ota["archive"]["object_count"])
            version_dir = root / "dist" / "v0.1.1"
            self.assertTrue((version_dir / "watche-sd-resources-v0.1.1.tar.gz").is_file())
            self.assertTrue((version_dir / "catalog.json").is_file())
            self.assertTrue((version_dir / "mobile_catalog.json").is_file())
            self.assertTrue((version_dir / "mobile" / "gif" / "boot.gif").is_file())
            self.assertFalse((version_dir / "desktop_catalog.json").exists())
            self.assertFalse((version_dir / "official_catalog.json").exists())

            with tarfile.open(version_dir / "watche-sd-resources-v0.1.1.tar.gz", "r:gz") as archive:
                members = set(archive.getnames())
            self.assertIn("anim/boot.animpack", members)
            self.assertIn("actions/boot.json", members)
            self.assertIn("sfx/boot.pcm", members)
            self.assertIn("catalog.json", members)
            self.assertIn("fixed_states.json", members)
            self.assertIn("resource_manifest.json", members)
            self.assertFalse(any(name.startswith("assets/") for name in members))

            self.assertEqual("mobile_catalog.json", ota["mobile"]["catalog"]["name"])
            self.assertEqual(9, ota["mobile"]["assets"]["count"])
            self.assertEqual("global", ota["region"])
            self.assertTrue(ota["mobile"]["assets"]["base_url"].startswith("https://raw.githubusercontent.com/"))
            cn_root = root / "dist" / "cn" / "WatcherRobot" / "sd"
            cn = PIPELINE.read_json(cn_root / "latest.json")
            self.assertEqual("cn", cn["region"])
            self.assertNotIn("github", json.dumps(cn))
            self.assertNotIn("download.orulink.cn", json.dumps(ota))
            for asset in (ota["archive"], ota["catalog"], ota["mobile"]["catalog"]):
                self.assertEqual(asset["url"], asset["github_url"])
            self.assertEqual(ota["mobile"]["assets"]["base_url"], ota["mobile"]["assets"]["github_base_url"])
            self.assertNotIn("tos_", json.dumps(ota))
            self.assertEqual(cn["archive"]["sha256"], ota["archive"]["sha256"])
            self.assertEqual("https://download.orulink.cn/WatcherRobot/sd/v0.1.1/mobile/gif/", cn["mobile"]["assets"]["base_url"])
            self.assertEqual("v0.1.1", PIPELINE.read_json(cn_root / "releases.json")["releases"][0]["version"])

            later = root / "later.json"
            PIPELINE.write_json(later, {"releases": [{"version": "v0.10.0", "published_at": ota["published_at"]}]})
            with self.assertRaisesRegex(PIPELINE.ResourceError, "roll back"):
                PIPELINE.stage_cn_release(version_dir, ota, cn_root, later)
            (version_dir / "mobile/gif/boot.gif").write_bytes(b"broken")
            with self.assertRaisesRegex(PIPELINE.ResourceError, "GIF differs"):
                PIPELINE.stage_cn_release(version_dir, ota, cn_root, None)

    def test_rejects_stale_mobile_gif_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_source(root)
            bundle = root / "bundle"
            mobile = root / "mobile"
            PIPELINE.build_resources(source, source / "sound", bundle, mobile, "v0.0.11")
            (mobile / "gif" / "removed_expression.gif").write_bytes(b"stale")

            with self.assertRaisesRegex(PIPELINE.ResourceError, "Mobile file set"):
                PIPELINE.validate_resources(source, bundle, mobile)

    def test_rejects_wrong_gif_dimensions_before_writing_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_source(root)
            Image.new("RGB", (128, 128), "red").save(source / "gif" / "boot.gif")
            with self.assertRaisesRegex(PIPELINE.ResourceError, "device requires 206x206"):
                PIPELINE.build_resources(
                    source,
                    source / "sound",
                    root / "bundle",
                    root / "mobile",
                    "v0.0.11",
                )

    def test_action_normalization_clamps_current_hardware_head_range(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            action = Path(temporary) / "happy.json"
            action.write_text(
                json.dumps(
                    {
                        "scene_name": "Scene",
                        "frame_start": 0,
                        "frame_end": 10,
                        "fps": 10,
                        "animated_objects": [
                            {
                                "keyframe_data": [
                                    {"frame_number": 0, "active_axis": "x", "rotation_angle": 95.7},
                                    {"frame_number": 10, "active_axis": "z", "rotation_angle": 181},
                                ]
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            normalized, adjustments = PIPELINE.normalize_action(action)
            angles = [
                frame["rotation_angle"]
                for item in normalized["animated_objects"]
                for frame in item["keyframe_data"]
            ]
            self.assertEqual([180, 100], angles)
            self.assertEqual(2, len(adjustments))

    def test_version_sequence_starts_at_v001_and_checked_in_index_resets_to_v0010(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / "index.json"
            self.assertEqual("v0.0.1", PIPELINE.next_version(index))
            index.write_text(
                '{"schema_version":1,"latest":"v0.0.3","versions":["v0.0.1","v0.0.3"]}',
                encoding="utf-8",
            )
            self.assertEqual("v0.0.4", PIPELINE.next_version(index))

        checked_in = SCRIPT.parents[1] / "official" / "releases" / "index.json"
        self.assertEqual("v0.1.1", PIPELINE.next_version(checked_in))
        self.assertFalse((SCRIPT.parents[1] / "official" / "releases" / "v0.0.11.json").exists())
        self.assertFalse((SCRIPT.parents[1] / "official" / "desktop").exists())

    def test_checked_in_mobile_catalog_matches_source_snapshot(self) -> None:
        repository = SCRIPT.parents[1]
        mobile = repository / "official" / "mobile"
        catalog = PIPELINE.read_json(mobile / "mobile_catalog.json")
        snapshot = PIPELINE.read_json(
            repository / "official" / "source" / "feishu-snapshot.json"
        )
        PIPELINE.schema_validate(
            catalog,
            "mobile-catalog.schema.json",
            "official/mobile/mobile_catalog.json",
        )
        source_records = sorted(snapshot["records"], key=lambda item: item["order"])
        self.assertEqual(
            [(record["resource_id"], record["display_name"]) for record in source_records],
            [(item["id"], item["display_name"]) for item in catalog["expressions"]],
        )
        expected_files = {"mobile_catalog.json"}
        expected_files.update(item["preview"] for item in catalog["expressions"])
        actual_files = {
            path.relative_to(mobile).as_posix()
            for path in mobile.rglob("*")
            if path.is_file()
        }
        self.assertEqual(expected_files, actual_files)
        for item in catalog["expressions"]:
            preview = mobile / item["preview"]
            self.assertEqual(item["preview_size"], preview.stat().st_size)
            self.assertEqual(item["preview_sha256"], PIPELINE.sha256_file(preview))

    def test_animpack_v3_roundtrip_and_rejects_v2_and_predictor_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_source(root)
            bundle = root / "bundle"
            mobile = root / "mobile"
            result = PIPELINE.build_resources(
                source, source / "sound", bundle, mobile, "v0.0.11"
            )
            self.assertEqual("animpack-v3", result["animation_format"])
            self.assertTrue(result["prefetch_stall_free_all"])
            self.assertLess(result["encoded_anim_bytes"], result["raw_anim_bytes"])
            pack_v3 = bundle / "anim" / "boot.animpack"
            header_v3, frames_v3 = PIPELINE.decode_animpack(pack_v3)
            self.assertEqual(3, header_v3["version"])
            self.assertEqual(1, len(frames_v3))
            policy = PIPELINE.load_policy()
            expected_raw = [
                PIPELINE.rgba_to_rgb565(image)
                for image, _ in PIPELINE.read_gif(source / "gif" / "boot.gif", policy)
            ]
            self.assertEqual(expected_raw, frames_v3)

            # Mutate the header version from 3 -> 2 -> legacy v2 must be rejected
            legacy_bytes = bytearray(pack_v3.read_bytes())
            legacy_bytes[4:6] = (2).to_bytes(2, "little")
            legacy_pack = root / "legacy_v2.animpack"
            legacy_pack.write_bytes(bytes(legacy_bytes))
            with self.assertRaisesRegex(PIPELINE.ResourceError, "Unsupported AnimPack header"):
                PIPELINE.decode_animpack(legacy_pack)

            # Mutate the embedded EFC1 header byte[5] to set predictor bit 0x08 -> must be rejected
            raw = bytearray(pack_v3.read_bytes())
            efc_pos = raw.index(b"EFC1")
            raw[efc_pos + 5] |= 0x08
            bad_pack = root / "bad_predictor.animpack"
            bad_pack.write_bytes(bytes(raw))
            with self.assertRaisesRegex(PIPELINE.ResourceError, "Unsupported predictor-plane EFC stream"):
                PIPELINE.decode_animpack(bad_pack)

    def test_animpack_v3_splits_high_entropy_gif_within_esp32_segment_cap(self) -> None:
        import numpy as np

        rng = np.random.default_rng(20261127)
        frames = []
        # 12 frames of uncompressible random RGB noise (> 1 MB total if unsplit)
        for _ in range(12):
            arr = rng.integers(0, 256, size=(206, 206, 3), dtype=np.uint8)
            frames.append((Image.fromarray(arr, mode="RGB").convert("RGBA"), 100))

        with tempfile.TemporaryDirectory() as temporary:
            pack = Path(temporary) / "noisy.animpack"
            stats = PIPELINE.write_animpack_efc(pack, frames, fps=10)
            self.assertGreater(stats["segments"], 1)
            self.assertLessEqual(stats["max_segment_bytes"], 450 * 1024)
            self.assertLessEqual(stats["max_segment_bytes"], PIPELINE.EFC_MAX_SEGMENT_BYTES)
            header, decoded = PIPELINE.decode_animpack(pack)
            self.assertEqual(3, header["version"])
            self.assertEqual(12, len(decoded))
            expected = [PIPELINE.rgba_to_rgb565(img) for img, _ in frames]
            self.assertEqual(expected, decoded)


if __name__ == "__main__":
    unittest.main()
