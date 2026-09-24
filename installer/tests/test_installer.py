"""Testy průvodce instalace (unittest; spustíš i přes pytest).

    python3 -m unittest discover -s installer/tests -v

End-to-end test zkopíruje repozitář do dočasné složky, spustí proti ní falešnou
Ollamu (:11434) a OpenWebUI (:8080) a projde celého průvodce se skriptovanými
odpověďmi. Skutečný config se nikdy nemění.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALLER = HERE.parent
ROOT = INSTALLER.parent
sys.path.insert(0, str(INSTALLER))
sys.path.insert(0, str(HERE))

from hans_setup import cfg as C            # noqa: E402
from hans_setup import household, models, network, persona  # noqa: E402
import fake_services                        # noqa: E402


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


class TestFreshPrivate(unittest.TestCase):
    def test_placeholders_removed(self):
        priv = C.fresh_private_from_example()
        flat = json.dumps(priv, ensure_ascii=False)
        self.assertNotIn(":0000", flat)
        self.assertNotIn('"uzivatel"', flat)
        self.assertNotIn("00000000-0000", flat)
        for sec in ("known_persons", "relationship_seed", "person_name_forms"):
            self.assertNotIn(sec, priv)
        # prázdná cesta k ArcFace by přebila rozumný default v kódu
        self.assertIsNone(C.get(priv, "hailo.recog_hef"))
        self.assertIsNone(C.get(priv, "greeting.user_prompt"))


class TestNetwork(unittest.TestCase):
    def test_apply_pc_ip_fresh(self):
        cfg = {}
        network.apply_pc_ip(cfg, "10.0.0.5")
        self.assertEqual(C.get(cfg, "openwebui_chat.base_url"), "http://10.0.0.5:11434")
        self.assertEqual(C.get(cfg, "openwebui_direct.base_url"), "http://10.0.0.5:8080")
        self.assertEqual(C.get(cfg, "voice.stt_url"),
                         "http://10.0.0.5:8080/api/v1/audio/transcriptions")
        self.assertEqual(C.get(cfg, "pc_remote.host"), "10.0.0.5")
        self.assertEqual(C.get(cfg, "wol_pc_ip"), "10.0.0.5")

    def test_apply_pc_ip_keeps_custom_port(self):
        cfg = {"openwebui_direct": {"base_url": "http://10.0.0.5:3000"}}
        network.apply_pc_ip(cfg, "10.0.0.9")
        self.assertEqual(C.get(cfg, "openwebui_direct.base_url"), "http://10.0.0.9:3000")

    def test_ip_validation(self):
        self.assertTrue(network._valid_ip("192.168.1.20"))
        self.assertFalse(network._valid_ip("192.168.1.300"))
        self.assertFalse(network._valid_ip("pc.local"))


class TestModels(unittest.TestCase):
    def test_model_paths(self):
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        paths = models.model_paths(cfg)
        hc = [p for p, m in paths.items() if m == "hans-czech:latest"]
        self.assertGreaterEqual(len(hc), 10)
        self.assertIn("models.dialog", paths)
        self.assertNotIn("gemini.model", paths)
        self.assertNotIn("openrouter.model", paths)
        self.assertFalse(any("image_model" in p for p in paths))
        self.assertNotIn("voice.wake_model", paths)

    def test_normalize(self):
        from hans_setup.ollama import normalize
        self.assertEqual(normalize("bge-m3"), "bge-m3:latest")
        self.assertEqual(normalize("a/b:7b"), "a/b:7b")
        self.assertEqual(normalize("jobautomation/OpenEuroLLM-Czech"),
                         "jobautomation/OpenEuroLLM-Czech:latest")


class TestPersona(unittest.TestCase):
    B = {"name": "Rozárka", "gender": "žena", "role": "zahradnice",
         "description": "x", "formal": True}

    def test_normalize_replaces_name(self):
        p = persona.normalize(dict(fake_services.PERSONA,
                                   core="Jsi Rozárka, zahradnice domácnosti. Máš ráda květiny."),
                              self.B)
        self.assertTrue(p["core"].startswith("Jsi {name}, zahradnice"))
        self.assertEqual(persona.problems(p, self.B), [])

    def test_problems_detected(self):
        p = persona.normalize(dict(fake_services.PERSONA, core="Jsi {name}. Jsi laskavá 🌻."), self.B)
        bad = " | ".join(persona.problems(p, self.B))
        self.assertIn("roli", bad)
        self.assertIn("emoji", bad)

    def test_language_rules(self):
        r = persona.language_rules(self.B, "Mluvíš krátce.")
        self.assertIn("ženského rodu", r)
        self.assertIn("ženském rodě", r)
        self.assertIn("vykáš", r)
        self.assertIn("Mluvíš česky", r)

    def test_slug(self):
        self.assertEqual(persona.slug("Rozárka Nová"), "rozarka-nova")


class TestHousehold(unittest.TestCase):
    def test_key_and_rules(self):
        self.assertEqual(household.key_of("Šárka Nová"), "sarka")
        people = [{"nom": "Karel", "voc": "Karle"}, {"nom": "Marie", "voc": "Marie"}]
        r = household.address_rules(people)
        self.assertIn("vokativ Karle (ne Karel)", r)
        self.assertNotIn("Marie (ne", r)

    def test_fallback_forms(self):
        # bez LLM se použijí Hansova pravidla z cz_names (jen základní vzory;
        # proto je průvodce vždy dá uživateli potvrdit)
        self.assertEqual(household._fallback_forms("Karel", "muž")["voc"], "Karle")
        f = household._fallback_forms("Honza", "muž")
        self.assertEqual((f["voc"], f["acc"]), ("Honzo", "Honzu"))


# ── end-to-end ──────────────────────────────────────────────────────────────
DRIVER = textwrap.dedent("""
    import builtins, json, sys
    feed = iter(json.loads(sys.argv[1]))
    prompts = []
    def fake_input(p=""):
        try:
            a = next(feed)
        except StopIteration:
            a = ""
        prompts.append(p)
        print(p + a)
        return a
    builtins.input = fake_input
    sys.stdin.isatty = lambda: True
    sys.path.insert(0, sys.argv[2])
    import wizard
    rc = wizard.main(sys.argv[3:])
    sys.exit(rc)
""")


@unittest.skipUnless(_port_free(11434) and _port_free(8080), "porty 11434/8080 obsazené")
class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import http.server
        import threading
        cls.st = fake_services.FakeState()
        cls.srvs = []
        for port in (11434, 8080):
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                                  fake_services.make_handler(cls.st))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            cls.srvs.append(srv)
        cls.tmp = Path(tempfile.mkdtemp(prefix="hans-installer-test-"))
        for name in ("config.json", "config.private.example.json"):
            shutil.copy2(ROOT / name, cls.tmp / name)
        for d in ("scripts", "tools", "installer"):
            shutil.copytree(ROOT / d, cls.tmp / d,
                            ignore=shutil.ignore_patterns("__pycache__", "data"))

    @classmethod
    def tearDownClass(cls):
        for s in cls.srvs:
            s.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def wiz(self, answers, *args):
        r = subprocess.run(
            [sys.executable, "-c", DRIVER, json.dumps(answers),
             str(self.tmp / "installer"), *args],
            cwd=self.tmp, capture_output=True, text=True, timeout=120)
        if os.environ.get("VERBOSE"):
            print(r.stdout, r.stderr)
        return r

    def test_full_flow(self):
        r = self.wiz(["127.0.0.1", "sk-test1234567890", "a@b.cz", "karel",
                      "aa:bb:cc:dd:ee:ff", "", "", ""], "network")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.tmp / "config.private.json").exists())

        r = self.wiz([], "llm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OpenEuroLLM", r.stdout)

        r = self.wiz(["Karel", "m", "pán domu", "", "", "", "", "",
                      "Eva", "z", "paní domu", "", "", "", "", "", "",   # "" = konec seznamu
                      "",                                              # rodinné vazby? ne
                      "Rozárka", "z", "zahradnice", "Laskavá zahradnice.", "",
                      "veverka skeptička", "", "",
                      ""], "persona")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        r = self.wiz(["1"], "models")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        r = self.wiz([], "knowledge")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        # ── výsledný config tak, jak ho načte Hans ──
        sys.path.insert(0, str(self.tmp))
        from scripts import config_io
        cfg = config_io.load(root=self.tmp)
        pub = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        priv = json.loads((self.tmp / "config.private.json").read_text(encoding="utf-8"))

        self.assertEqual(cfg["persona"]["name"], "Rozárka")
        self.assertTrue(cfg["persona"]["core"].startswith("Jsi {name}, zahradnice"))
        self.assertIn("ženského rodu", cfg["persona"]["language_rules"])
        self.assertIn("vokativ Karle (ne Karel)", cfg["persona"]["address_rules"])
        self.assertIn("vokativ Evo (ne Eva)", cfg["persona"]["address_rules"])
        self.assertEqual(cfg["known_persons"]["karel"]["voc"], "Karle")
        self.assertEqual(cfg["known_persons"]["eva"]["dat"], "Evě")
        self.assertEqual(cfg["relationship_seed"]["eva"]["role"], "paní domu")
        self.assertEqual(cfg["hans_dialog"]["kolac_name"], "Šiška")
        self.assertEqual(cfg["tts"]["voice"], "cs-CZ-VlastaNeural")
        self.assertEqual(cfg["models"]["dialog"], "rozarka-czech:latest")
        left = {p: m for p, m in models.model_paths(cfg).items() if "hans-czech" in m}
        self.assertEqual(left, {})
        self.assertIn("rozarka-czech:latest", self.st.models)
        self.assertEqual(len(cfg["knowledge"]["collections"]), 6)
        self.assertTrue(cfg["knowledge"]["enabled"])
        self.assertEqual(cfg["openwebui_direct"]["api_token"], "sk-test1234567890")
        self.assertEqual(cfg["wol_pc_mac"], "aa:bb:cc:dd:ee:ff")
        self.assertFalse(cfg["kodi"]["enabled"])
        # tajemství a jména NESMÍ být ve veřejném souboru
        self.assertEqual(config_io.zkontroluj_verejny(pub, priv), [])
        self.assertNotIn("sk-test", json.dumps(pub))
        self.assertNotIn("known_persons", pub)
        # dokumenty identity nahrané do kolekce hans_identita
        uploads = [b for m, p, b in self.st.log if p == "/api/v1/files/"]
        self.assertEqual(len(uploads), 4)
        # hans_persona z toho postaví systémový prompt se jménem
        from scripts.hans_persona import persona_core
        core = persona_core(cfg)
        self.assertIn("Jsi Rozárka, zahradnice", core)
        self.assertIn("Karle", core)


class TestDryRun(unittest.TestCase):
    def test_dry_run_does_not_touch_real_config(self):
        before = {p: (ROOT / p).read_bytes() for p in ("config.json",)
                  if (ROOT / p).exists()}
        priv_before = (ROOT / "config.private.json").exists()
        r = subprocess.run(["bash", str(INSTALLER / "install.sh"), "--dry-run", "--yes",
                            "--fresh", "--skip", "llm,persona,models"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for p, data in before.items():
            self.assertEqual((ROOT / p).read_bytes(), data, p)
        self.assertEqual((ROOT / "config.private.json").exists(), priv_before)
        self.assertIn("[dry-run]", r.stdout)
        shutil.rmtree(ROOT / "data" / "installer" / "dryrun", ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
