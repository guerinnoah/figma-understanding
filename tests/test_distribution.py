import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "figma-understanding"


class DistributionTests(unittest.TestCase):
    def test_skill_is_self_contained_and_discoverable(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        self.assertRegex(frontmatter, r"(?m)^name: figma-understanding$")
        self.assertRegex(frontmatter, r"(?m)^license: MIT$")
        self.assertRegex(frontmatter, r"(?m)^compatibility: .+")
        self.assertFalse(re.search(r"python3 scripts/figma_tool\.py", text))
        self.assertTrue((SKILL / "scripts" / "figma_tool.py").is_file())
        self.assertTrue((SKILL / "scripts" / "figma_understanding" / "cli.py").is_file())

        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(SKILL / "scripts" / "figma_tool.py"), "doctor"],
                cwd=directory,
                text=True,
                capture_output=True,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("figma-understanding 0.1.0", result.stdout)

    def test_plugin_manifests_and_openai_metadata_exist(self):
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual("figma-understanding", claude["name"])
        self.assertEqual("./skills", codex["skills"])
        metadata = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("$figma-understanding", metadata)


if __name__ == "__main__":
    unittest.main()
