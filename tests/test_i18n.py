"""Translation table consistency and language resolution, evaluated with a JS runtime.

I18n.js is a QML `.pragma library` file; stripping the pragma leaves plain JS.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = shutil.which("bun") or shutil.which("node")


def run_js(expression):
    source = (ROOT / "I18n.js").read_text().replace(".pragma library", "", 1)
    script = source + f"\nprocess.stdout.write(JSON.stringify({expression}));\n"
    result = subprocess.run([RUNTIME, "-e", script] if RUNTIME.endswith("node") else [RUNTIME, "-e", script],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


@unittest.skipUnless(RUNTIME, "needs bun or node")
class I18nTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.strings = run_js("strings")

    def test_every_language_has_every_key_with_the_same_placeholders(self):
        english = self.strings["en"]
        self.assertGreaterEqual(len(self.strings), 14)
        for lang, table in self.strings.items():
            self.assertEqual(set(table), set(english), lang)
            for key, text in table.items():
                self.assertEqual(sorted(re.findall(r"\{\d+\}", text)), sorted(re.findall(r"\{\d+\}", english[key])),
                                 f"{lang}.{key}")
                self.assertTrue(text.strip(), f"{lang}.{key} is empty")
                self.assertNotRegex(text, r"[<>&]", f"{lang}.{key} contains markup characters")

    def test_resolution_precedence(self):
        cases = [
            (["auto", {}], "en"),
            (["auto", {"LANG": "cs_CZ.UTF-8"}], "cs"),
            (["auto", {"LANG": "C"}], "en"),
            (["auto", {"LANG": "POSIX", "LC_MESSAGES": "de_AT.UTF-8"}], "de"),
            (["auto", {"LANGUAGE": "xx:sk:cs", "LANG": "en_US"}], "sk"),
            (["auto", {"LANGUAGE": "", "LC_ALL": "C", "LANG": "ja_JP.UTF-8"}], "ja"),
            (["auto", {"LC_ALL": "pt_BR.UTF-8@euro", "LANG": "de_DE"}], "pt"),
            (["auto", {"LANG": "zh-TW"}], "zh"),
            (["fr", {"LANG": "de_DE"}], "fr"),
            (["  AUTO ", {"LANG": "pl_PL"}], "pl"),
            (["klingon", {"LANG": "ru_RU"}], "ru"),
            (["klingon", {}], "en"),
            ([None, {"LANG": "uk_UA"}], "uk"),
        ]
        results = run_js("[" + ",".join(f"resolve({json.dumps(a)}, {json.dumps(b)})" for (a, b), _ in cases) + "]")
        for ((args), expected), got in zip(cases, results):
            self.assertEqual(got, expected, args)

    def test_formatting_fallbacks(self):
        got = run_js('[tr("cs","free",[2,4]), tr("xx","free",[1,2]), tr("de","noSuchKey"),'
                     ' tr("en","free",["{1}","x"]), tr("ja","inTime",[0])]')
        # Substituted text is not re-scanned for placeholders.
        self.assertEqual(got, ["volné 2/4", "free 1/2", "noSuchKey", "free {1}/x", "0後"])


if __name__ == "__main__":
    unittest.main()
