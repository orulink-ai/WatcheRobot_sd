from __future__ import annotations

import importlib.util
import json
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
                    "loop": resource_id != "boot",
                    "gif": f"token-{resource_id}",
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
        return source

    def test_build_validate_and_package_device_compatible_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.create_source(root)
            bundle = root / "bundle"
            desktop = root / "desktop"
            result = PIPELINE.build_resources(source, source / "sound", bundle, desktop, "v0.0.1")
            self.assertEqual(8, result["expressions"])
            self.assertTrue((bundle / "anim" / "boot.animpack").is_file())
            self.assertTrue((bundle / "anim" / "anim_manifest.bin").is_file())
            self.assertEqual(
                set(self.fixed_states),
                set(json.loads((bundle / "behavior" / "states.json").read_text())["states"]),
            )
            validated = PIPELINE.validate_resources(source, bundle, desktop)
            self.assertEqual(8, validated["expressions"])
            ota = PIPELINE.package_resources(bundle, root / "dist", "v0.0.1")
            self.assertRegex(ota["archive"]["sha256"], r"^[a-f0-9]{64}$")
            self.assertTrue((root / "dist" / "v0.0.1" / "watche-sd-resources-v0.0.1.tar.gz").is_file())

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

    def test_version_sequence_starts_at_v001(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = Path(temporary) / "index.json"
            self.assertEqual("v0.0.1", PIPELINE.next_version(index))
            index.write_text(
                '{"schema_version":1,"latest":"v0.0.3","versions":["v0.0.1","v0.0.3"]}',
                encoding="utf-8",
            )
            self.assertEqual("v0.0.4", PIPELINE.next_version(index))


if __name__ == "__main__":
    unittest.main()
