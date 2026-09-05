#!/usr/bin/env python3
"""Safely merge Kunlun Gateway into an OpenCode V1 project config.

The helper never accepts or persists an API key. OpenCode resolves the key from
``KUNLUN_GATEWAY_API_KEY`` at runtime. The default mode is a read-only preview.
"""

from __future__ import annotations

import argparse
from importlib import resources
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any
from urllib.parse import urlparse


PROVIDER_ID = "kunlun-gateway"
PLUGIN_FILENAME = "kunlun-gateway-idempotency.js"


def plugin_asset_bytes() -> bytes:
    return resources.files("scripts").joinpath("assets", "kunlun_gateway_idempotency.js").read_bytes()


def plugin_destination(target: Path) -> Path:
    return target.parent / ".opencode" / "plugins" / PLUGIN_FILENAME


def ensure_plugin_can_install(target: Path) -> Path:
    destination = plugin_destination(target)
    if destination.is_symlink():
        raise ValueError("OpenCode 幂等插件是符号链接，拒绝覆盖")
    if destination.exists() and destination.read_bytes() != plugin_asset_bytes():
        raise ValueError(f"OpenCode 幂等插件已存在且内容不同，拒绝覆盖：{destination}")
    return destination


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".kunlun-gateway-", suffix=".js", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def install_plugin_asset(target: Path) -> tuple[Path, bool]:
    destination = ensure_plugin_can_install(target)
    if destination.exists():
        return destination, False
    atomic_write_bytes(destination, plugin_asset_bytes())
    return destination, True


def provider_config(
    base_url: str,
    model: str,
    output_limit: int,
    context_limit: int = 128_000,
) -> dict[str, Any]:
    normalized = base_url.rstrip("/")
    try:
        parsed = urlparse(normalized)
        parsed.port
    except ValueError as exc:
        raise ValueError("baseURL 无效") from exc
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1", "localhost", "::1",
    }
    if parsed.scheme != "https" and not local_http:
        raise ValueError("baseURL 必须是 HTTPS，或本机 127.0.0.1/localhost 地址")
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("baseURL 禁止缺失主机、用户信息、查询或片段")
    if not normalized.endswith("/v1"):
        raise ValueError("baseURL 必须以 /v1 结尾")
    if not model or len(model) > 120:
        raise ValueError("模型 ID 无效")
    if output_limit < 1 or output_limit > 262_144:
        raise ValueError("输出 Token 上限无效")
    if context_limit < output_limit or context_limit > 2_000_000:
        raise ValueError("上下文 Token 上限必须不小于输出上限且不超过 2000000")
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
                "limit": {"context": context_limit, "output": output_limit},
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
    context_limit: int = 128_000,
) -> dict[str, Any]:
    merged = json.loads(json.dumps(current))
    merged.setdefault("$schema", "https://opencode.ai/config.json")
    providers = merged.setdefault("provider", {})
    if not isinstance(providers, dict):
        raise ValueError("现有 provider 字段不是对象，拒绝覆盖")
    providers[PROVIDER_ID] = provider_config(
        base_url, model, output_limit, context_limit,
    )
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
        if path.is_symlink():
            raise ValueError("目标配置是符号链接，拒绝修改")
        backup = path.with_suffix(path.suffix + ".pre-kunlun-gateway.bak")
        if backup.is_symlink() or (backup.exists() and not backup.is_file()):
            raise ValueError("OpenCode 配置备份不是普通文件，拒绝覆盖")
        source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(path, source_flags)
        backup_fd, backup_name = tempfile.mkstemp(
            prefix=".opencode-kunlun-backup-", suffix=".bak", dir=path.parent,
        )
        temporary_backup = Path(backup_name)
        try:
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size > 2 * 1024 * 1024:
                raise ValueError("目标配置不是可安全备份的普通文件")
            with os.fdopen(source_fd, "rb") as source, os.fdopen(backup_fd, "wb") as destination:
                source_fd = -1
                backup_fd = -1
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(temporary_backup, stat.S_IMODE(source_stat.st_mode))
            os.replace(temporary_backup, backup)
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if backup_fd >= 0:
                os.close(backup_fd)
            if temporary_backup.exists():
                temporary_backup.unlink()
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
    parser.add_argument("--context-limit", type=int, default=128_000)
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
        context_limit=args.context_limit,
    )
    plugin_path = ensure_plugin_can_install(target)
    if not args.apply:
        print(json.dumps(merged, ensure_ascii=False, indent=2))
        print(f"\n只读预览：将安装幂等插件到 {plugin_path}；未修改文件。确认后增加 --apply。")
        return 0
    installed_plugin, plugin_was_installed = install_plugin_asset(target)
    if current == merged:
        print(f"配置无变更：{target}")
    else:
        backup = atomic_write_with_backup(target, merged)
        print(f"已更新：{target}")
        if backup:
            print(f"可恢复备份：{backup}")
    print(f"{'已安装' if plugin_was_installed else '已存在'}幂等插件：{installed_plugin}")
    print("API Key 未写入配置；请在启动 OpenCode 的环境中设置 KUNLUN_GATEWAY_API_KEY。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
