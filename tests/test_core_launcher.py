import importlib.util
import json
from pathlib import Path


def launcher():
    path = Path(__file__).parents[1] / 'scripts/run_core_capture.py'
    spec = importlib.util.spec_from_file_location('core_launcher', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_prints_diagnostics_after_main_finishes(tmp_path, monkeypatch, capsys):
    module = launcher()
    path = tmp_path / 'core.json'
    def main(args):
        assert args[:4] == ['--capture-backend', 'core', '--core-diagnostics', str(path)]
        assert args[4:] == ['--iface', 'wlanpi0']
        path.write_text(json.dumps(dict(received_bytes=1000, decoded_records=9,
            normalized_frames=9, core_stop_confirmed=True, restoration_verified=True)))
        return 0
    monkeypatch.setattr(module, 'main', main)
    assert module.run(['--core-diagnostics', str(path), '--iface', 'wlanpi0']) == 0
    stderr = capsys.readouterr().err
    assert 'Received 1000 bytes; decoded 9 rows; normalized 9 frames.' in stderr
    assert 'Core stop confirmed: True; radio restoration verified: True.' in stderr


def test_launcher_does_not_present_an_old_report_as_this_run(tmp_path, monkeypatch, capsys):
    module = launcher()
    path = tmp_path / 'core.json'
    path.write_text(json.dumps(dict(restoration_verified=True)))
    monkeypatch.setattr(module, 'main', lambda args: 1)
    assert module.run(['--core-diagnostics', str(path)]) == 1
    assert not capsys.readouterr().err
