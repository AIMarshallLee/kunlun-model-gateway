from __future__ import annotations

import json

import pytest

from scripts.opencode_install import atomic_write_with_backup, load_target, main, merge_config


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
    assert "gw_" not in json.dumps(merged)


def test_opencode_merge_rejects_insecure_remote_url():
    with pytest.raises(ValueError, match="HTTPS"):
        merge_config(
            {}, base_url="http://public.example/v1", model="test-model", output_limit=4096,
            set_default_model=True,
        )


def test_opencode_writer_creates_recoverable_backup(tmp_path):
    target = tmp_path / "opencode.json"
    target.write_text('{"existing": true}\n', encoding="utf-8")
    backup = atomic_write_with_backup(target, {"new": True})
    assert backup is not None
    assert json.loads(backup.read_text(encoding="utf-8")) == {"existing": True}
    assert load_target(target) == {"new": True}


def test_opencode_apply_requires_explicit_target_and_writes_only_there(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["--apply"])
    assert not (tmp_path / "opencode.json").exists()

    target = tmp_path / "workspace" / "opencode.json"
    assert main(["--target", str(target), "--apply"]) == 0
    assert load_target(target)["provider"]["kunlun-gateway"]
    assert not (tmp_path / "opencode.json").exists()
