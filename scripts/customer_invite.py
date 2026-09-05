"""One-attempt private onboarding; never pass secrets on the command line."""

import argparse
import getpass
import re
import sys
from urllib.parse import urlsplit

import httpx


def validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("仅接受无用户名、路径、查询和片段的 HTTPS 站点地址")
    return value.rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser(description="向已核验客户签发一次性开通/恢复链接，不自动重试")
    parser.add_argument("--origin", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--email")
    target.add_argument("--recover-user")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--reason", required=True, help="10-500 字符，不填写个人资料或密钥")
    parser.add_argument("--identity-confirmed", action="store_true", required=True)
    parser.add_argument("--vercel", action="store_true", help="额外输入运维入口门禁密钥")
    args = parser.parse_args()
    try:
        origin = validate_origin(args.origin)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", args.operation_id):
            raise ValueError("operation-id 无效")
        if args.recover_user and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", args.recover_user):
            raise ValueError("用户 ID 无效")
        if not 10 <= len(args.reason) <= 500:
            raise ValueError("reason 必须为 10-500 字符")
    except ValueError as exc:
        parser.error(str(exc))
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("请在可信交互终端运行，禁止管道、重定向或自动日志采集")
    headers = {"X-Kunlun-Ops-Token": getpass.getpass("短期 accounts:invite 运维令牌：")}
    if args.vercel:
        headers["X-Kunlun-Ops-Ingress-Secret"] = getpass.getpass("Vercel 运维入口密钥：")
    payload = {"operation_id": args.operation_id, "reason": args.reason, "identity_confirmed": True}
    path = "/ops/accounts/invitations"
    if args.email:
        payload["email"] = args.email
    else:
        path = f"/ops/accounts/{args.recover_user}/recovery"
    try:
        # No redirect or retry: a lost response might already have created an account.
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            result = client.post(origin + path, headers=headers, json=payload)
        if result.status_code != 201:
            print(f"未取得开通凭证（HTTP {result.status_code}）。核查原 operation-id，勿自动重试。")
            return 1
        data = result.json()
        if not isinstance(data, dict) or not isinstance(data.get("user_id"), str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", data["user_id"]):
            raise ValueError("invalid response")
        activation_path = data.get("activation_path", "")
        if not re.fullmatch(r"/reset-password#token=reset_[A-Za-z0-9_-]+", activation_path):
            raise ValueError("invalid response")
    except (httpx.HTTPError, ValueError, TypeError):
        print("结果不确定。先按原 operation-id 核查账户/审计；必要时用独立恢复操作签发新链接。")
        return 1
    finally:
        headers.clear()
    print("一次性链接（1 小时有效）：仅发给已核验客户，不粘贴到工单、公开聊天或日志。")
    print(origin + activation_path)
    print("账户 ID：" + str(data["user_id"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
