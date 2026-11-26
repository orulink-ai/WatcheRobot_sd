import tempfile
import unittest
from pathlib import Path
import test_resource_pipeline as _trp

PIPELINE = _trp.PIPELINE


class LocalizedMobileTests(unittest.TestCase):
    def test_localized_display_names_do_not_change_device_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _trp.ResourcePipelineTests().create_source(root)
            snapshot_path = source / "feishu-snapshot.json"
            snapshot = PIPELINE.read_json(snapshot_path)
            for rec in snapshot["records"]:
                rec.pop("display_names", None)
            PIPELINE.write_json(snapshot_path, snapshot)

            bundle = root / "bundle"
            mobile_dir = root / "mobile"

            def build():
                PIPELINE.build_resources(source, source / "sound", bundle, mobile_dir, "v0.0.11")
                return (
                    PIPELINE.read_json(mobile_dir / "mobile_catalog.json"),
                    PIPELINE.read_json(bundle / "catalog.json"),
                )

            old_mobile, old_device = build()
            names = {"zh-Hans": "待机", "en": "Standby", "ja": "待機"}
            snapshot["records"][1]["display_names"] = names
            PIPELINE.write_json(snapshot_path, snapshot)

            mobile, device = build()
            self.assertEqual(device, old_device)
            self.assertEqual(mobile["expressions"][1]["display_names"], names)
            self.assertEqual(mobile["expressions"][1]["device"], {"image_name": "standby"})
            self.assertNotIn("display_names", mobile["expressions"][0])
            self.assertNotEqual(mobile["content_hash"], old_mobile["content_hash"])
            ota = PIPELINE.package_resources(bundle, mobile_dir, root / "dist", "v0.0.11")
            published = root / "dist/v0.0.11/mobile_catalog.json"
            self.assertEqual(ota["mobile"]["catalog"]["sha256"], PIPELINE.sha256_file(published))
            self.assertNotIn("desktop", ota)


if __name__ == "__main__":
    unittest.main()
