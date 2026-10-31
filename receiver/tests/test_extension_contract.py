"""The GNOME extension's D-Bus XML must declare every method (with matching signature) the receiver calls."""
import json, re, shutil, subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
import pytest

EXT = Path(__file__).resolve().parents[2] / "gnome-extension"
SRC = (EXT / "droptouch@droptouch.dev/extension.js").read_text()

EXPECTED = {"Approach": "s", "Begin": "sx", "Progress": "sd", "Finish": "ssb", "ShowPairing": "ss",
            "HidePairing": "", "DragUpdate": "ddb", "DragCancel": "", "Fling": "sdddddi",
    "ShowPairRequest": "ssi", "HidePairRequest": "",
            "PhoneLink": "sb", "OutDone": "b", "SendOut": "sissddd",
            "FlingEdge": "ssdddddi", "LiveDragStart": "ssdd", "LiveDragMove": "sdd", "LiveDragEnd": "b"}


def test_xml_matches_bridge():
    node = ET.fromstring(re.search(r"<node>.*</node>", SRC, re.S).group(0))
    got = {m.get("name"): "".join(a.get("type") for a in m.findall("arg")) for m in node.iter("method")}
    assert got == EXPECTED
    bridge = (Path(__file__).resolve().parents[1] / "shell_bridge.py").read_text()
    for name in EXPECTED:
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        assert f'"{snake}"' in bridge or f"call_{snake}" in bridge, snake


def test_metadata_supports_gnome_50():
    meta = json.loads((EXT / "droptouch@droptouch.dev/metadata.json").read_text())
    assert "50" in meta["shell-version"]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_extension_physics_node():
    r = subprocess.run(["node", "--test", str(EXT / "test")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
