#!/usr/bin/env python3
"""Mint a short-lived scoped token from the trusted operations plane."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.services.ops_tokens import ALLOWED_SCOPES, OpsTokenError, mint_operator_token


def _secret() -> str:
    direct = os.getenv("KUNLUN_OPERATOR_SIGNING_SECRET")
    file_name = os.getenv("KUNLUN_OPERATOR_SIGNING_SECRET_FILE")
    if direct and file_name:
        raise OpsTokenError("运维签名密钥来源冲突")
    if file_name:
        path = Path(file_name)
        if not path.is_absolute() or not path.is_file():
            raise OpsTokenError("运维签名密钥文件无效")
        return path.read_text(encoding="utf-8").strip()
    return direct or ""


def main() -> int:
    parser = argparse.ArgumentParser(description="签发昆仑网关短期运维令牌")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--scope", action="append", required=True, choices=sorted(ALLOWED_SCOPES))
    parser.add_argument("--ttl", type=int, default=300)
    args = parser.parse_args()
    try:
        token = mint_operator_token(
            _secret(),
            subject=args.subject,
            scopes=set(args.scope),
            ttl_seconds=args.ttl,
        )
    except (OSError, OpsTokenError) as exc:
        parser.error(str(exc))
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
