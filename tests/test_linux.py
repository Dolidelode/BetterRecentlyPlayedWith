"""What the app does differently on Linux (also checked on Windows by pretending to be Linux)."""
import os

from brpw import chat_ui, game, msauth, paths, updater


def test_sea_of_thieves_through_proton_is_found_in_proc(tmp_path):
    for pid, comm, cmd in (("10", "bash", b"/bin/bash\x00-l"),
                           ("11", "SoTGame.exe", b"Z:\\steam\\SoTGame.exe\x00-x"), ("x", "no", b"")):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "comm").write_text(comm + "\n")
        (tmp_path / pid / "cmdline").write_bytes(cmd)
    names = game.linux_processes(str(tmp_path))
    assert "sotgame.exe" in names and "bash" in names and game.game_running(names)
    assert not game.game_running({"bash"}) and game.linux_processes(str(tmp_path / "nope")) == set()


def test_sign_ins_outside_windows_live_in_a_file_only_you_can_read(tmp_path, monkeypatch):
    monkeypatch.setattr(msauth.sys, "platform", "linux")
    blob = msauth.protect("refresh-token")
    assert blob.startswith(msauth.PLAIN) and msauth.unprotect(blob) == "refresh-token"
    try:
        msauth.unprotect("AQAAANCMnd8BFdERjHoAwE")                # a Windows-encrypted one can't be read here
        assert False
    except OSError:
        pass


def test_data_folder_follows_xdg(tmp_path, monkeypatch):
    for v in ("BRPW_DATA_DIR", "APPDATA"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path / "BetterRecentlyPlayedWith"


def test_no_self_replacing_update_outside_windows(monkeypatch):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "platform", "linux")
    assert updater.running_exe() is None                         # the release page is offered instead


def test_mouse_wheel_on_linux_scrolls_too():
    got = []

    class W:
        def __init__(self):
            self.binds, self.alls = {}, {}

        def bind(self, seq, fn, add=None):
            self.binds[seq] = fn

        def bind_all(self, seq, fn):
            self.alls[seq] = fn

        def unbind_all(self, seq):
            self.alls.pop(seq, None)
    w = W()
    chat_ui.wheel(w, lambda e: got.append(e.delta))
    w.binds["<Enter>"](None)
    ev = type("E", (), {"x": 1, "y": 2, "widget": w})()
    w.alls["<Button-4>"](ev)
    w.alls["<Button-5>"](ev)
    assert got == [120, -120]
    w.binds["<Leave>"](None)
    assert not w.alls


def test_linux_bundle_uses_system_certificates_and_original_libraries():
    from brpw import fix_linux_bundle
    env = {"LD_LIBRARY_PATH": "/tmp/_MEI123", "LD_LIBRARY_PATH_ORIG": "/opt/mine"}
    fix_linux_bundle(env, frozen=True, platform="linux", exists=lambda f: f == "/etc/pki/tls/certs/ca-bundle.crt")
    assert env["SSL_CERT_FILE"] == "/etc/pki/tls/certs/ca-bundle.crt"
    assert env["LD_LIBRARY_PATH"] == "/opt/mine"

    env = {"LD_LIBRARY_PATH": "/tmp/_MEI123", "SSL_CERT_FILE": "/my/certs.pem"}
    fix_linux_bundle(env, frozen=True, platform="linux", exists=lambda f: True)
    assert env == {"SSL_CERT_FILE": "/my/certs.pem"}          # the user's own choice stays; no bundle path leaks

    env = {"LD_LIBRARY_PATH": "/x"}
    fix_linux_bundle(env, frozen=False, platform="linux", exists=lambda f: True)
    fix_linux_bundle(env, frozen=True, platform="win32", exists=lambda f: True)
    assert env == {"LD_LIBRARY_PATH": "/x"}                   # from source, and on Windows: untouched
