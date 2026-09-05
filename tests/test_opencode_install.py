from __future__ import annotations

import json

import pytest

from scripts.opencode_install import (
    PLUGIN_FILENAME,
    atomic_write_with_backup,
    load_target,
    main,
    plugin_asset_bytes,
    plugin_destination,
    merge_config,
)


def test_opencode_merge_preserves_product_mcp_and_never_embeds_key():
    current = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"chrome-devtools": {"enabled": False}},
        "permission": {"chrome-devtools_*": "ask"},
    }
    merged = merge_config(
        current,
        base_url="http://127.0.0.1:8787/v1",
        model="test-model",
        output_limit=4096,
        set_default_model=False,
    )
    assert merged["mcp"] == current["mcp"]
    assert merged["permission"] == current["permission"]
    assert "model" not in merged
    provider = merged["provider"]["kunlun-gateway"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["apiKey"] == "{env:KUNLUN_GATEWAY_API_KEY}"
    assert provider["models"]["test-model"]["limit"] == {
        "context": 128_000,
        "output": 4096,
    }
    assert "gw_" not in json.dumps(merged)


def test_opencode_merge_rejects_insecure_remote_url():
    with pytest.raises(ValueError, match="HTTPS"):
        merge_config(
            {}, base_url="http://public.example/v1", model="test-model", output_limit=4096,
            set_default_model=True,
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:secret@evil.example/v1",
        "https:///v1",
        "https://gateway.example/v1?key=secret",
        "https://gateway.example/v1#fragment",
    ],
)
def test_opencode_merge_rejects_ambiguous_or_credentialed_url(base_url):
    with pytest.raises(ValueError, match="baseURL"):
        merge_config(
            {}, base_url=base_url, model="test-model", output_limit=4096,
            set_default_model=True,
        )


def test_opencode_merge_rejects_context_smaller_than_output():
    with pytest.raises(ValueError, match="上下文 Token"):
        merge_config(
            {}, base_url="https://gateway.example/v1", model="test-model",
            output_limit=4096, context_limit=2048, set_default_model=True,
        )


def test_opencode_writer_creates_recoverable_backup(tmp_path):
    target = tmp_path / "opencode.json"
    target.write_text('{"existing": true}\n', encoding="utf-8")
    backup = atomic_write_with_backup(target, {"new": True})
    assert backup is not None
    assert json.loads(backup.read_text(encoding="utf-8")) == {"existing": True}
    assert load_target(target) == {"new": True}


def test_opencode_writer_refuses_backup_symlink_without_touching_victim(tmp_path):
    target = tmp_path / "opencode.json"
    target.write_text('{"existing": true}\n', encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("KEEP\n", encoding="utf-8")
    backup = target.with_suffix(".json.pre-kunlun-gateway.bak")
    backup.symlink_to(victim)

    with pytest.raises(ValueError, match="备份不是普通文件"):
        atomic_write_with_backup(target, {"new": True})

    assert victim.read_text(encoding="utf-8") == "KEEP\n"
    assert load_target(target) == {"existing": True}


def test_opencode_apply_requires_explicit_target_and_writes_only_there(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["--apply"])
    assert not (tmp_path / "opencode.json").exists()

    target = tmp_path / "workspace" / "opencode.json"
    assert main(["--target", str(target), "--apply"]) == 0
    assert load_target(target)["provider"]["kunlun-gateway"]
    assert not (tmp_path / "opencode.json").exists()


def test_opencode_preview_never_writes_config_or_plugin(tmp_path, capsys):
    target = tmp_path / "workspace" / "opencode.json"

    assert main(["--target", str(target)]) == 0

    assert not target.exists()
    assert not plugin_destination(target).exists()
    assert str(plugin_destination(target)) in capsys.readouterr().out


def test_opencode_apply_installs_packaged_plugin_and_is_idempotent(tmp_path):
    target = tmp_path / "workspace" / "opencode.json"

    assert main(["--target", str(target), "--apply"]) == 0
    plugin = plugin_destination(target)
    assert plugin.name == PLUGIN_FILENAME
    assert plugin.read_bytes() == plugin_asset_bytes()
    first_config = target.read_bytes()
    first_plugin = plugin.read_bytes()

    assert main(["--target", str(target), "--apply"]) == 0
    assert target.read_bytes() == first_config
    assert plugin.read_bytes() == first_plugin
    assert not target.with_suffix(".json.pre-kunlun-gateway.bak").exists()


def test_opencode_apply_refuses_to_overwrite_different_plugin(tmp_path):
    target = tmp_path / "workspace" / "opencode.json"
    plugin = plugin_destination(target)
    plugin.parent.mkdir(parents=True)
    plugin.write_text("export default () => ({})\n", encoding="utf-8")

    with pytest.raises(ValueError, match="内容不同"):
        main(["--target", str(target), "--apply"])

    assert plugin.read_text(encoding="utf-8") == "export default () => ({})\n"
    assert not target.exists()
