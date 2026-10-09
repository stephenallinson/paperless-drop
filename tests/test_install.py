import configparser
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from paperless_drop.install import THUNAR_ID, InstallError, Installer


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home))
        self.calls = []

        def run(argv, **kwargs):
            self.calls.append(argv)

            class P:
                returncode = 0
                stdout = ""
            return P()

        self.inst = Installer(self.home, exe=["/opt/my tools/paperless-drop"], run=run, say=lambda s: None)

    def parse_desktop(self, path):
        cp = configparser.RawConfigParser(strict=False)
        cp.optionxform = str
        cp.read(path)
        return cp

    def test_openwith_entry(self):
        self.inst.install_openwith()
        cp = self.parse_desktop(self.inst.openwith_path)
        self.assertEqual(cp["Desktop Entry"]["Exec"], '"/opt/my tools/paperless-drop" send %F')
        # A declared MimeType would make this a candidate default handler for PDFs and images.
        self.assertNotIn("MimeType", cp["Desktop Entry"])

    def test_dolphin_menu_is_executable(self):
        self.inst.install_dolphin()
        self.assertTrue(self.inst.dolphin_path.stat().st_mode & 0o100)
        cp = self.parse_desktop(self.inst.dolphin_path)
        self.assertEqual(cp["Desktop Action sendToPaperless"]["Name"], "Send to Paperless")
        self.assertEqual(cp["Desktop Entry"]["Actions"], "sendToPaperless;")

    def test_thunar_preserves_existing_actions_and_is_idempotent(self):
        self.inst.thunar_path.parent.mkdir(parents=True)
        original = ('<?xml version="1.0" encoding="UTF-8"?>\n<actions>\n<action>\n'
                    '\t<icon>utilities-terminal</icon>\n\t<name>Open Terminal Here</name>\n'
                    '\t<unique-id>111-1</unique-id>\n\t<command>term %f</command>\n'
                    '\t<patterns>*</patterns>\n\t<directories/>\n</action>\n</actions>\n')
        self.inst.thunar_path.write_text(original)
        self.inst.install_thunar()
        self.inst.install_thunar()
        actions = ET.parse(self.inst.thunar_path).getroot().findall("action")
        self.assertEqual([a.findtext("name") for a in actions], ["Open Terminal Here", "Send to Paperless"])
        ours = actions[1]
        self.assertEqual(ours.findtext("unique-id"), THUNAR_ID)
        self.assertIn("*.pdf", ours.findtext("patterns"))
        self.assertTrue(self.inst.thunar_path.with_name("uca.xml.paperless-drop.bak").exists())
        self.assertEqual(self.inst.thunar_path.with_name("uca.xml.paperless-drop.bak").read_text(), original)

    def test_thunar_created_when_missing(self):
        self.inst.install_thunar()
        self.assertEqual(len(ET.parse(self.inst.thunar_path).getroot().findall("action")), 1)

    def test_watcher_unit(self):
        self.inst.install_watcher()
        text = self.inst.service_path.read_text()
        self.assertIn('ExecStart="/opt/my tools/paperless-drop" watch', text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("RestartPreventExitStatus=2", text)
        self.assertIn(["systemctl", "--user", "enable", "--now", "paperless-drop.service"], self.calls)

    def test_uninstall_removes_only_ours(self):
        self.inst.install_thunar()
        other = ET.parse(self.inst.thunar_path)
        extra = ET.SubElement(other.getroot(), "action")
        ET.SubElement(extra, "name").text = "Mine"
        ET.SubElement(extra, "unique-id").text = "999"
        other.write(self.inst.thunar_path)
        self.inst.install_openwith()
        self.inst.install_dolphin()
        self.inst.install_watcher()
        self.inst.uninstall()
        self.assertEqual(set(self.inst.status().values()), {False})
        self.assertEqual([a.findtext("name") for a in ET.parse(self.inst.thunar_path).getroot()], ["Mine"])
        self.assertIn(["systemctl", "--user", "disable", "--now", "paperless-drop.service"], self.calls)

    # -- Nautilus -------------------------------------------------------------------------------
    def test_nautilus_script(self):
        self.inst.install_nautilus()
        path = self.inst.nautilus_path
        self.assertTrue(path.stat().st_mode & 0o100)
        text = path.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("exec '/opt/my tools/paperless-drop' send \"$@\"", text)
        self.assertIn('[ "$#" -gt 0 ] || exit 0', text)  # right-click on the background
        self.inst.install_nautilus()  # idempotent
        self.assertTrue(self.inst.status()["nautilus"])

    def test_nautilus_script_runs_with_spaces_in_names(self):
        import subprocess
        fake = self.home / "fake-paperless-drop"
        fake.write_text('#!/bin/sh\nfor a in "$@"; do echo "[$a]"; done\n')
        fake.chmod(0o755)
        inst = Installer(self.home, exe=[str(fake)], run=lambda *a, **k: None, say=lambda s: None)
        inst.install_nautilus()
        out = subprocess.run([str(inst.nautilus_path), "my file.pdf", "b.pdf"], capture_output=True, text=True)
        self.assertEqual(out.stdout.split("\n")[:3], ["[send]", "[my file.pdf]", "[b.pdf]"])
        self.assertEqual(subprocess.run([str(inst.nautilus_path)]).returncode, 0)  # no selection: no-op

    def test_nautilus_refuses_to_overwrite_someone_elses_script(self):
        self.inst.nautilus_path.parent.mkdir(parents=True)
        self.inst.nautilus_path.write_text("#!/bin/sh\necho mine\n")
        with self.assertRaises(InstallError):
            self.inst.install_nautilus()
        self.inst.uninstall()
        self.assertEqual(self.inst.nautilus_path.read_text(), "#!/bin/sh\necho mine\n")

    # -- COSMIC Files ---------------------------------------------------------------------------
    def cosmic_entries(self):
        """Names of the actions in the file (a crude but sufficient RON read for these tests)."""
        import re
        text = re.sub(r"//[^\n]*", "", self.inst.cosmic_path.read_text())
        return re.findall(r'name:\s*"([^"]*)"', text)

    USER_ACTION = '(name: "Mine", confirm: true, selection: Any, steps: ["/bin/true %F"])'

    def test_cosmic_creates_file(self):
        self.inst.install_cosmic()
        text = self.inst.cosmic_path.read_text()
        self.assertTrue(text.startswith("[\n") and text.rstrip().endswith("]"))
        self.assertIn('steps: [\n            "\\"/opt/my tools/paperless-drop\\" send %F",', text)
        self.assertIn("selection: Files", text)
        self.assertEqual(self.cosmic_entries(), ["Send to Paperless"])

    def test_cosmic_merges_with_existing_entries(self):
        for existing in (f"[{self.USER_ACTION}]", f"[\n    {self.USER_ACTION},\n]\n", "[]", "[\n]\n"):
            with self.subTest(existing=existing):
                self.inst.cosmic_path.parent.mkdir(parents=True, exist_ok=True)
                self.inst.cosmic_path.write_text(existing)
                self.inst.install_cosmic()
                self.inst.install_cosmic()  # idempotent: one block only
                names = self.cosmic_entries()
                self.assertEqual(names.count("Send to Paperless"), 1)
                self.assertEqual("Mine" in names, "Mine" in existing)
                self.assertEqual(self.inst.cosmic_path.read_text().count("paperless-drop: begin"), 1)

    def test_cosmic_backup_goes_outside_the_version_directory(self):
        self.inst.cosmic_path.parent.mkdir(parents=True)
        self.inst.cosmic_path.write_text(f"[{self.USER_ACTION}]")
        self.inst.install_cosmic()
        backup = self.inst.cosmic_path.parent.parent / "context_actions.paperless-drop.bak"
        self.assertEqual(backup.read_text(), f"[{self.USER_ACTION}]")
        self.assertEqual([p.name for p in self.inst.cosmic_path.parent.iterdir()], ["context_actions"])

    def test_cosmic_refuses_ambiguous_files_without_modifying_them(self):
        self.inst.cosmic_path.parent.mkdir(parents=True)
        for content in ("not a list", f"[{self.USER_ACTION},\n    // my note\n]"):
            with self.subTest(content=content):
                self.inst.cosmic_path.write_text(content)
                with self.assertRaises(InstallError) as ctx:
                    self.inst.install_cosmic()
                self.assertIn("Send to Paperless", str(ctx.exception))  # prints the snippet to add by hand
                self.assertEqual(self.inst.cosmic_path.read_text(), content)

    def test_cosmic_uninstall_keeps_user_entries(self):
        self.inst.cosmic_path.parent.mkdir(parents=True)
        self.inst.cosmic_path.write_text(f"[{self.USER_ACTION}]")
        self.inst.install_cosmic()
        self.inst.uninstall()
        self.assertEqual(self.cosmic_entries(), ["Mine"])
        self.assertNotIn("paperless-drop", self.inst.cosmic_path.read_text())

    def test_cosmic_uninstall_removes_file_we_created(self):
        self.inst.install_cosmic()
        self.inst.uninstall()
        self.assertFalse(self.inst.cosmic_path.exists())


if __name__ == "__main__":
    unittest.main()
