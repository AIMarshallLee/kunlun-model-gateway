#!/usr/bin/env python3
"""Safely merge Kunlun Gateway into an OpenCode V1 project config.

The helper never accepts or persists an API key. OpenCode resolves the key from
``KUNLUN_GATEWAY_API_KEY`` at runtime. The default mode is a read-only preview.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any


PROVIDER_ID = "kunlun-gateway"


def provider_config(base_url: str, model: str, output_limit: int) -> dict[str, Any]:
    normalized = base_url.rstrip("/")
    if not normalized.startswith(("http://127.0.0.1:", "http://localhost:", "https://")):
        raise ValueError("baseURL 必须是 HTTPS，或本机 127.0.0.1/localhost 地址")
    if not normalized.endswith("/v1"):
        raise ValueError("baseURL 必须以 /v1 结尾")
    if not model or len(model) > 120:
        raise ValueError("模型 ID 无效")
    if output_limit < 1 or output_limit > 262_144:
        raise ValueError("输出 Token 上限无效")
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Kunlun Gateway",
        "options": {
            "baseURL": normalized,
            "apiKey": "{env:KUNLUN_GATEWAY_API_KEY}",
        },
        "models": {
            model: {
                "name": f"Kunlun {model}",
                "limit": {"output": output_limit},
            }
        },
    }


def merge_config(
    current: dict[str, Any],
    *,
    base_url: str,
    model: str,
    output_limit: int,
    set_default_model: bool,
) -> dict[str, Any]:
    merged = json.loads(json.dumps(current))
    merged.setdefault("$schema", "https://opencode.ai/config.json")
    providers = merged.setdefault("provider", {})
    if not isinstance(providers, dict):
        raise ValueError("现有 provider 字段不是对象，拒绝覆盖")
    providers[PROVIDER_ID] = provider_config(base_url, model, output_limit)
    if set_default_model:
        merged["model"] = f"{PROVIDER_ID}/{model}"
    return merged


def load_target(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("目标配置是符号链接，拒绝修改")
    if not path.exists():
        return {"$schema": "https://opencode.ai/config.json"}
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("目标配置超过 2 MiB，拒绝处理")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("OpenCode 配置根节点必须是对象")
    return data


def atomic_write_with_backup(path: Path, data: dict[str, Any]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".pre-kunlun-gateway.bak")
        shutil.copy2(path, backup, follow_symlinks=False)
    fd, temporary_name = tempfile.mkstemp(prefix=".opencode-kunlun-", suffix=".json", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预览或安装 Kunlun Gateway 的 OpenCode V1 Provider 配置")
    parser.add_argument(
        "--target",
        type=Path,
        help="OpenCode 配置文件路径；--apply 时必须显式提供，避免写错工作区",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8787/v1")
    parser.add_argument("--model", default="test-model")
    parser.add_argument("--output-limit", type=int, default=4096)
    parser.add_argument("--set-default-model", action="store_true")
    parser.add_argument("--apply", action="store_true", help="显式写入；省略时只预览")
    args = parser.parse_args(argv)
    if args.apply and args.target is None:
        parser.error("--apply 必须显式提供 --target，拒绝推断 OpenCode 工作区")
    target = args.target or (Path.cwd() / "opencode.json")
    current = load_target(target)
    merged = merge_config(
        current,
        base_url=args.base_url,
        model=args.model,
        output_limit=args.output_limit,
        set_default_model=args.set_default_model,
    )
    if not args.apply:
        print(json.dumps(merged, ensure_ascii=False, indent=2))
        print("\n只读预览：未修改文件。确认后增加 --apply。")
        return 0
    backup = atomic_write_with_backup(target, merged)
    print(f"已更新：{target}")
    if backup:
        print(f"可恢复备份：{backup}")
    print("API Key 未写入配置；请在启动 OpenCode 的环境中设置 KUNLUN_GATEWAY_API_KEY。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
