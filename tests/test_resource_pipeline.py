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
                    "resource_id": resource_id,
                    "order": order,
                    "gif": f"token-{resource_id}",
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
                "resource_id": future_id,
                "order": future_order,
                "gif": "token-future-expression",
                "action": None,
                "sound": None,
            }
        )
        (source / "feishu-snapshot.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "Watcher expression overview",
                    "imported_at": "2026-07-30T00:00:00Z",
                    "records": records,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
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
        records[0]["action"] = "token-boot-action"
        records[0]["sound"] = "token-boot-sound"
        (source / "feishu-snapshot.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "Watcher expression overview",
                    "imported_at": "2026-07-30T00:00:00Z",
                    "records": records,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return source

    def test_build_validate_and_package_device_compatible_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_source(root)
            bundle = root / "bundle"
            desktop = root / "desktop"
            result = PIPELINE.build_resources(source, source / "sound", bundle, desktop, "v0.0.1")
            self.assertEqual(9, result["expressions"])
            catalog = json.loads((bundle / "official_catalog.json").read_text())
            boot = next(item for item in catalog["expressions"] if item["id"] == "boot")
            self.assertNotIn("loop", boot)
            boot_hash = boot["assets"]["animation"]["sha256"]
            boot_pack = bundle / "assets" / "anim" / f"{boot_hash}.animpack"
            self.assertTrue(boot_pack.is_file())
            boot_header, _ = PIPELINE.decode_animpack(boot_pack)
            self.assertEqual(0, boot_header["loop"])
            self.assertEqual(
                set(self.fixed_states),
                set(json.loads((bundle / "fixed_states.json").read_text())["states"]),
            )
            validated = PIPELINE.validate_resources(source, bundle, desktop)
            self.assertEqual(9, validated["expressions"])
            self.assertEqual(1, validated["dynamic_expressions"])
            self.assertEqual(9, validated["device_first_frames"])
            desktop_catalog = json.loads((desktop / "desktop_catalog.json").read_text())
            self.assertEqual(3, desktop_catalog["schema_version"])
            self.assertEqual("watche-desktop-resource-catalog", desktop_catalog["format"])
            self.assertEqual("v0.0.1", desktop_catalog["version"])
            desktop_boot = next(
                item for item in desktop_catalog["expressions"] if item["id"] == "boot"
            )
            self.assertNotIn("loop", desktop_boot)
            self.assertEqual("actions/boot.json", desktop_boot["creator"]["action"]["path"])
            self.assertEqual("sounds/boot.pcm", desktop_boot["creator"]["sound"]["path"])
            self.assertTrue((desktop / "actions" / "boot.json").is_file())
            self.assertTrue((desktop / "sounds" / "boot.pcm").is_file())
            mobile = root / "mobile"
            mobile_catalog = json.loads((mobile / "mobile_catalog.json").read_text())
            self.assertEqual(1, mobile_catalog["schema_version"])
            self.assertEqual("watche-mobile-expression-catalog", mobile_catalog["format"])
            self.assertEqual("v0.0.1", mobile_catalog["version"])
            mobile_boot = next(
                item for item in mobile_catalog["expressions"] if item["id"] == "boot"
            )
            self.assertEqual("gif/boot.gif", mobile_boot["preview"])
            self.assertEqual(
                PIPELINE.sha256_file(source / "gif" / "boot.gif"),
                mobile_boot["preview_sha256"],
            )
            self.assertEqual(
                (source / "gif" / "boot.gif").stat().st_size,
                mobile_boot["preview_size"],
            )
            self.assertTrue((mobile / "gif" / "boot.gif").is_file())
            ota = PIPELINE.package_resources(bundle, desktop, root / "dist", "v0.0.1")
            self.assertEqual(3, ota["schema_version"])
            self.assertEqual(2, ota["layout_revision"])
            self.assertEqual("WRSD/2", ota["protocol"])
            self.assertGreater(ota["archive"]["expanded_size"], 0)
            self.assertGreater(ota["archive"]["file_count"], 0)
            self.assertEqual(11, ota["archive"]["object_count"])
            self.assertRegex(ota["archive"]["sha256"], r"^[a-f0-9]{64}$")
            self.assertEqual("watche-sd-resources-v0.0.1.tar.gz", ota["archive"]["name"])
            self.assertEqual(
                "https://github.com/orulink-ai/WatcheRobot_sd/releases/download/v0.0.1/"
                "watche-sd-resources-v0.0.1.tar.gz",
                ota["archive"]["github_url"],
            )
            self.assertEqual(
                "https://erroright.tos-cn-guangzhou.volces.com/WatcherRobot/sd/v0.0.1/"
                "watche-sd-resources-v0.0.1.tar.gz",
                ota["archive"]["tos_url"],
            )
            self.assertEqual("official_catalog.json", ota["catalog"]["name"])
            version_dir = root / "dist" / "v0.0.1"
            self.assertTrue((version_dir / "watche-sd-resources-v0.0.1.tar.gz").is_file())
            self.assertEqual("desktop_catalog.json", ota["desktop"]["catalog"]["name"])
            self.assertEqual(
                "watche-desktop-resources-v0.0.1.tar.gz",
                ota["desktop"]["archive"]["name"],
            )
            desktop_archive = version_dir / ota["desktop"]["archive"]["name"]
            self.assertTrue(desktop_archive.is_file())
            with tarfile.open(desktop_archive, "r:gz") as archive:
                members = sorted(archive.getnames())
            self.assertIn("desktop_catalog.json", members)
            self.assertIn("previews/boot.webp", members)
            self.assertIn("actions/boot.json", members)
            self.assertIn("sounds/boot.pcm", members)
            self.assertEqual(12, ota["desktop"]["archive"]["file_count"])
            self.assertEqual(
                "https://github.com/orulink-ai/WatcheRobot_sd/releases/download/v0.0.1/"
                "watche-desktop-resources-v0.0.1.tar.gz",
                ota["desktop"]["archive"]["github_url"],
            )
            self.assertEqual(
                "https://erroright.tos-cn-guangzhou.volces.com/WatcherRobot/sd/v0.0.1/"
                "desktop_catalog.json",
                ota["desktop"]["catalog"]["tos_url"],
            )
            self.assertEqual("mobile_catalog.json", ota["mobile"]["catalog"]["name"])
            self.assertEqual(9, ota["mobile"]["assets"]["count"])
            self.assertEqual("individual-gif", ota["mobile"]["assets"]["format"])
            self.assertEqual(
                "https://raw.githubusercontent.com/orulink-ai/WatcheRobot_sd/v0.0.1/"
                "official/mobile/gif/",
                ota["mobile"]["assets"]["github_base_url"],
            )
            self.assertEqual(
                "https://erroright.tos-cn-guangzhou.volces.com/WatcherRobot/sd/v0.0.1/"
                "mobile/gif/",
                ota["mobile"]["assets"]["tos_base_url"],
            )
            self.assertTrue((version_dir / "mobile_catalog.json").is_file())
            self.assertTrue((version_dir / "mobile" / "gif" / "boot.gif").is_file())

    def test_rejects_stale_desktop_preview_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_source(root)
            bundle = root / "bundle"
            desktop = root / "desktop"
            PIPELINE.build_resources(source, source / "sound", bundle, desktop, "v0.0.1")
            (desktop / "previews" / "removed_expression.webp").write_bytes(b"stale")

            with self.assertRaisesRegex(PIPELINE.ResourceError, "Desktop file set"):
                PIPELINE.validate_resources(source, bundle, desktop)

    def test_rejects_stale_mobile_gif_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_source(root)
            bundle = root / "bundle"
            desktop = root / "desktop"
            PIPELINE.build_resources(source, source / "sound", bundle, desktop, "v0.0.1")
            (root / "mobile" / "gif" / "removed_expression.gif").write_bytes(b"stale")

            with self.assertRaisesRegex(PIPELINE.ResourceError, "Mobile file set"):
                PIPELINE.validate_resources(source, bundle, desktop)

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
                    root / "desktop",
                    "v0.0.1",
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

    def test_work_manifest_links_official_and_user_assets_by_hash(self) -> None:
        sha256 = "a" * 64
        work = {
            "schema_version": 1,
            "work_id": "morning_show",
            "name": "早安组合",
            "duration_ms": 3000,
            "tracks": [
                {
                    "type": "animation",
                    "start_ms": 0,
                    "duration_ms": 3000,
                    "asset": {
                        "source": "official",
                        "resource_id": "happy",
                        "kind": "anim",
                        "sha256": sha256,
                        "size": 1024,
                        "format": "animpack-v2",
                    },
                },
                {
                    "type": "sound",
                    "start_ms": 0,
                    "asset": {
                        "source": "user",
                        "kind": "sfx",
                        "sha256": "b" * 64,
                        "size": 2048,
                        "format": "pcm-s16le-24khz-mono",
                    },
                },
            ],
        }
        PIPELINE.schema_validate(work, "work-manifest.schema.json", "work")
        work["tracks"][0]["asset"].pop("resource_id")
        with self.assertRaisesRegex(PIPELINE.ResourceError, "resource_id"):
            PIPELINE.schema_validate(work, "work-manifest.schema.json", "work")

    def test_version_sequence_starts_at_v001(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / "index.json"
            self.assertEqual("v0.0.1", PIPELINE.next_version(index))
            index.write_text(
                '{"schema_version":1,"latest":"v0.0.3","versions":["v0.0.1","v0.0.3"]}',
                encoding="utf-8",
            )
            self.assertEqual("v0.0.4", PIPELINE.next_version(index))

    def test_published_v001_manifest_remains_schema_compatible(self) -> None:
        manifest = PIPELINE.read_json(
            SCRIPT.parents[1] / "official" / "releases" / "v0.0.1.json"
        )
        PIPELINE.schema_validate(manifest, "ota-manifest.schema.json", "v0.0.1")

    def test_checked_in_desktop_catalog_matches_source_snapshot(self) -> None:
        repository = SCRIPT.parents[1]
        desktop = repository / "official" / "desktop"
        catalog = PIPELINE.read_json(desktop / "desktop_catalog.json")
        snapshot = PIPELINE.read_json(
            repository / "official" / "source" / "feishu-snapshot.json"
        )
        PIPELINE.schema_validate(
            catalog,
            "desktop-catalog.schema.json",
            "official/desktop/desktop_catalog.json",
        )
        source_records = sorted(snapshot["records"], key=lambda item: item["order"])
        self.assertEqual(
            [
                (record["resource_id"], record["display_name"], record["source_record_id"])
                for record in source_records
            ],
            [
                (item["id"], item["display_name"], item["source_record_id"])
                for item in catalog["expressions"]
            ],
        )
        expected_files = {"desktop_catalog.json"}
        for item in catalog["expressions"]:
            expected_files.add(item["preview"])
            expected_files.update(
                asset["path"] for asset in item.get("creator", {}).values()
            )
        actual_files = {
            path.relative_to(desktop).as_posix()
            for path in desktop.rglob("*")
            if path.is_file()
        }
        self.assertEqual(expected_files, actual_files)
        for item in catalog["expressions"]:
            self.assertEqual(
                item["preview_sha256"],
                PIPELINE.sha256_file(desktop / item["preview"]),
            )
            for asset in item.get("creator", {}).values():
                self.assertEqual(asset["sha256"], PIPELINE.sha256_file(desktop / asset["path"]))

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


if __name__ == "__main__":
    unittest.main()
