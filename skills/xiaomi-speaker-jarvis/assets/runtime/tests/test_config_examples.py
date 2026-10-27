import json
from pathlib import Path
import tempfile
import unittest

from home_jarvis.home import HomeAssistantGateway
from home_jarvis.planner import MiniMaxPlanner
from home_jarvis.speaker import SpeakerError, XiaomiSpeaker
from home_jarvis.xiaomi_cloud import XiaomiCloud


EXAMPLES = Path(__file__).resolve().parents[1] / "config-examples"


class ConfigExampleTests(unittest.TestCase):
    def test_templates_load_after_private_placeholder_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def private(name, value):
                path = root / name
                path.write_text(json.dumps(value) if isinstance(value, dict) else value)
                path.chmod(0o600)
                return path

            home = json.loads((EXAMPLES / "home.example.json").read_text())
            home["token_file"] = str(private("ha-token", "EXAMPLE_TEST_ONLY_TOKEN"))
            gateway = HomeAssistantGateway(private("home.json", home))
            self.assertEqual(gateway.resolve("打开示例灯")[0].entity_id, "switch.example_light")

            model = json.loads((EXAMPLES / "minimax.example.json").read_text())
            model["api_key_file"] = str(private("minimax.key", "EXAMPLE_TEST_ONLY_KEY"))
            planner = MiniMaxPlanner(private("minimax.json", model), gateway)
            self.assertEqual(planner.model, model["model"])

            speaker = json.loads((EXAMPLES / "speaker.example.json").read_text())
            with self.assertRaises(SpeakerError):
                XiaomiSpeaker(private("speaker.json", speaker))
            speaker.update(did="111111111", mac="02:00:00:00:00:01", token="a" * 32)
            local = XiaomiSpeaker(private("speaker.json", speaker))

            cloud = json.loads((EXAMPLES / "cloud.example.json").read_text())
            cloud["miotDID"] = speaker["did"]
            remote = XiaomiCloud(private("cloud.json", cloud))
            self.assertEqual(local.miot_did, remote.miot_did)


if __name__ == "__main__":
    unittest.main()
