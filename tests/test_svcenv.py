import importlib
import os


def load(tmp_path, text, monkeypatch, host="MYHOST"):
    p = tmp_path / "x.env"
    p.write_text(text)
    monkeypatch.setenv("SERVICES_ENV_FILE", str(p))
    import svcenv
    m = importlib.reload(svcenv)
    m.HOST = host
    return m


def test_host_prefix_wins_then_shared_then_default(tmp_path, monkeypatch):
    m = load(tmp_path, 'ROLE=hub\nMYHOST_ROLE=gpu-server\nQUOTED="a b c"\n# comment\nEMPTY=\n', monkeypatch)
    assert m.env("ROLE") == "gpu-server"
    m.HOST = "OTHER"
    assert m.env("ROLE") == "hub"
    assert m.env("QUOTED") == "a b c"
    assert m.env("EMPTY", "dflt") == "dflt"
    assert m.env("MISSING", 7) == 7


def test_process_environment_beats_the_file(tmp_path, monkeypatch):
    m = load(tmp_path, "HUB_API_URL=http://from-file\n", monkeypatch)
    monkeypatch.setenv("HUB_API_URL", "http://from-env")
    assert m.env("HUB_API_URL") == "http://from-env"


def teardown_module():
    import svcenv
    os.environ["SERVICES_ENV_FILE"] = os.path.join(os.path.dirname(__file__), "empty.env")
    importlib.reload(svcenv)
