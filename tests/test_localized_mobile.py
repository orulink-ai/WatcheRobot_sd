import tempfile, unittest
from pathlib import Path
from test_resource_pipeline import PIPELINE, ResourcePipelineTests

class LocalizedMobileTests(unittest.TestCase):
    def test_localized_display_names_do_not_change_device_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Path(tmp)
            source = ResourcePipelineTests().create_source(r)
            snapshot_path = source / "feishu-snapshot.json"
            snapshot = PIPELINE.read_json(snapshot_path)
            targets = dict(bundle=r/"bundle", desktop=r/"desktop", mobile=r/"mobile")
            def build():
                PIPELINE.build_resources(source, source/"sound", targets["bundle"], targets["desktop"], "v0.0.11", targets["mobile"])
                return (PIPELINE.read_json(targets["mobile"]/"mobile_catalog.json"), PIPELINE.read_json(targets["bundle"]/"official_catalog.json"))
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
            ota = PIPELINE.package_resources(targets["bundle"], targets["desktop"], r/"dist", "v0.0.11", targets["mobile"])
            published = r/"dist/v0.0.11/mobile_catalog.json"
            self.assertEqual(ota["mobile"]["catalog"]["sha256"], PIPELINE.sha256_file(published))
