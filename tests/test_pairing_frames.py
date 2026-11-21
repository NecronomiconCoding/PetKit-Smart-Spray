"""Frame-level checks for local pairing; runs without Home Assistant installed."""
import importlib.util, pathlib, sys, types

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "petkit_k3_local"

def _stub(name, **attrs):
    mod = types.ModuleType(name); mod.__dict__.update(attrs); sys.modules[name] = mod; return mod

_stub("bleak"); _stub("bleak.exc", BleakError=type("BleakError", (Exception,), {}))
_stub("bleak_retry_connector", BleakClientWithServiceCache=object, establish_connection=None)
_stub("homeassistant"); _stub("homeassistant.components", bluetooth=types.SimpleNamespace())
_stub("homeassistant.components.bluetooth"); _stub("homeassistant.core", HomeAssistant=object)
pkg = _stub("petkit_k3_local"); pkg.__path__ = [str(ROOT)]

def _load(name):
    spec = importlib.util.spec_from_file_location(f"petkit_k3_local.{name}", ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod); return mod

const = _load("const"); pairing = _load("pairing")

def test_frames_match_the_commands_the_device_is_known_to_accept():
    assert pairing.build_frame(pairing.CMD_GET_DEVICE_INFO, 0).hex() == const.INIT_CMD
    secret_hex = "a1b2c3d4e5f6"
    auth = pairing.build_frame(pairing.CMD_AUTH_VERIFY, 1, pairing.secret_to_payload(secret_hex)).hex()
    assert auth == const.AUTH_CMD_PREFIX + secret_hex + const.AUTH_CMD_SUFFIX
    assert pairing.build_frame(0xDC, 0x0A, bytes([1, 3])).hex() == const.SPRAY_CMD

def test_init_frame_layout():
    frame = pairing.build_frame(pairing.CMD_DEVICE_INIT, 1, bytes(range(8)) + b"\x00\x00" + bytes(6))
    assert frame[:3] == b"\xfa\xfc\xfd" and frame[3] == 73 and frame[6] == 16 and frame[-1] == 0xFB and len(frame) == 25

def test_parser_handles_split_noise_and_multiple_frames():
    a = bytes.fromhex("fafcfdd5020008000102030405060708fb"); b = bytes.fromhex("fafcfd4902010100" + "01" + "fb")
    buf = bytearray(b"\x00\x11" + a[:5]); assert pairing.parse_frames(buf) == []
    buf.extend(a[5:] + b)
    frames = pairing.parse_frames(buf)
    assert frames == [(0xD5, 2, 0, bytes.fromhex("0102030405060708")), (0x49, 2, 1, b"\x01")]
    assert buf == bytearray()

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"): fn(); print("ok", name)
