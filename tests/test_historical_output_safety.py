from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = (
    ("scripts.compare_cost_revision", "build_comparison"),
    ("scripts.compare_benchmark_adjustment", "build_comparison"),
    ("scripts.analyze_research_inference", "build_report"),
)


def _script(monkeypatch, module_name, builder_name, args):
    module = importlib.import_module(module_name)
    monkeypatch.setattr(sys, "argv", [module.__file__, *args])
    if hasattr(module, "_print_report"):
        monkeypatch.setattr(module, "_print_report", lambda _: None)
    return module


@pytest.mark.parametrize("module_name,builder_name", SCRIPTS)
def test_default_only_verifies_frozen_artifact_without_build_or_write(
    monkeypatch, capsys, module_name, builder_name
):
    module = _script(monkeypatch, module_name, builder_name, [])
    before = module.DEFAULT_OUTPUT.read_bytes()
    monkeypatch.setattr(
        module,
        builder_name,
        lambda **_: pytest.fail("default rebuilt historical artifact"),
    )
    monkeypatch.setattr(
        module,
        "write_json",
        lambda *a, **kw: pytest.fail("default wrote historical artifact"),
    )
    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "verified_immutable_artifact"
    assert result["sha256"] == module.IMMUTABLE_ARTIFACT_SHA256
    assert module.DEFAULT_OUTPUT.read_bytes() == before


@pytest.mark.parametrize("module_name,builder_name", SCRIPTS)
def test_default_rejects_changed_frozen_bytes_without_repair(
    tmp_path, monkeypatch, module_name, builder_name
):
    module = _script(monkeypatch, module_name, builder_name, [])
    corrupt = tmp_path / "changed.json"
    corrupt.write_text("changed historical bytes")
    monkeypatch.setattr(module, "DEFAULT_OUTPUT", corrupt)
    monkeypatch.setattr(
        module, builder_name, lambda **_: pytest.fail("attempted regeneration")
    )
    monkeypatch.setattr(
        module, "write_json", lambda *a, **kw: pytest.fail("attempted repair")
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        module.main()
    assert corrupt.read_text() == "changed historical bytes"


@pytest.mark.parametrize("module_name,builder_name", SCRIPTS)
@pytest.mark.parametrize(
    "target", ["existing", "data", "sources", "artifacts", "symlink"]
)
def test_explicit_existing_or_protected_output_refused_before_build(
    tmp_path, monkeypatch, module_name, builder_name, target
):
    root = tmp_path / "project"
    root.mkdir()
    if target == "existing":
        output = tmp_path / "existing.json"
        output.write_text("keep")
    elif target == "symlink":
        (root / "artifacts").mkdir()
        link = tmp_path / "alias"
        link.symlink_to(root / "artifacts", target_is_directory=True)
        output = link / "new.json"
    else:
        output = root / target / "new.json"
    module = _script(monkeypatch, module_name, builder_name, ["--output", str(output)])
    monkeypatch.setattr(module, "PROJECT_ROOT", root)
    monkeypatch.setattr(
        module, builder_name, lambda **_: pytest.fail("built for forbidden output")
    )
    with pytest.raises(ValueError, match="immutable"):
        module.main()
    if target == "existing":
        assert output.read_text() == "keep"
    else:
        assert not output.exists()


@pytest.mark.parametrize("module_name,builder_name", SCRIPTS)
def test_explicit_frozen_path_refused_without_writing(
    monkeypatch, module_name, builder_name
):
    module = importlib.import_module(module_name)
    output = module.DEFAULT_OUTPUT
    before = output.read_bytes()
    _script(monkeypatch, module_name, builder_name, ["--output", str(output)])
    monkeypatch.setattr(
        module, builder_name, lambda **_: pytest.fail("built for frozen path")
    )
    with pytest.raises(ValueError, match="immutable"):
        module.main()
    assert output.read_bytes() == before


@pytest.mark.parametrize("module_name,builder_name", SCRIPTS)
def test_explicit_new_output_succeeds_but_never_overwrites(
    tmp_path, monkeypatch, module_name, builder_name
):
    output = tmp_path / "fresh.json"
    module = _script(monkeypatch, module_name, builder_name, ["--output", str(output)])
    monkeypatch.setattr(
        module,
        builder_name,
        lambda **_: {"categories": [], "test_payload": 0.123456789123},
    )
    assert module.main() == 0
    before = output.read_bytes()
    assert json.loads(before)["test_payload"] == 0.1234567891
    with pytest.raises(ValueError, match="immutable"):
        module.main()
    assert output.read_bytes() == before


@pytest.mark.parametrize("module_name,builder_name", SCRIPTS)
def test_output_created_during_build_cannot_be_replaced(
    tmp_path, monkeypatch, module_name, builder_name
):
    output = tmp_path / "race.json"
    module = _script(monkeypatch, module_name, builder_name, ["--output", str(output)])

    def build(**_):
        output.write_text("another writer won")
        return {"categories": []}

    monkeypatch.setattr(module, builder_name, build)
    with pytest.raises(FileExistsError):
        module.main()
    assert output.read_text() == "another writer won"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["race.json"]


@pytest.mark.parametrize(
    "target", ["existing", "data", "sources", "artifacts", "symlink"]
)
def test_preserved_source_runner_refuses_existing_and_protected_paths(
    tmp_path, monkeypatch, target
):
    from scripts import replay_historical_learner_sources as module

    root = tmp_path / "project"
    root.mkdir()
    if target == "existing":
        output = tmp_path / "existing.json"
        output.write_text("preserved")
    elif target == "symlink":
        (root / "sources").mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(root / "sources", target_is_directory=True)
        output = alias / "new.json"
    else:
        output = root / target / "new.json"
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(
        module, "replay", lambda: pytest.fail("replay started for forbidden path")
    )
    monkeypatch.setattr(sys, "argv", [module.__file__, "--output", str(output)])
    with pytest.raises(ValueError, match="immutable"):
        module.main()
    if target == "existing":
        assert output.read_text() == "preserved"
    else:
        assert not output.exists()


def test_preserved_source_runner_writes_only_explicit_new_output(tmp_path, monkeypatch):
    from scripts import replay_historical_learner_sources as module

    output = tmp_path / "fresh.json"
    monkeypatch.setattr(module, "replay", lambda: {"results": {"checked": True}})
    monkeypatch.setattr(sys, "argv", [module.__file__, "--output", str(output)])
    assert module.main() == 0
    assert json.loads(output.read_text()) == {"results": {"checked": True}}
    with pytest.raises(ValueError, match="immutable"):
        module.main()
