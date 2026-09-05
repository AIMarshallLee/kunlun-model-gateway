"""Public request schemas. Unknown model fields are rejected explicitly in MVP."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .security import normalize_email


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterRequest(StrictModel):
    email: str
    password: str = Field(min_length=12, max_length=256)
    captcha_token: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)


class LoginRequest(StrictModel):
    email: str
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)


class EmailAddressRequest(StrictModel):
    email: str
    captcha_token: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)


class VerifyEmailRequest(StrictModel):
    token: str = Field(min_length=24, max_length=512)


class ResetPasswordRequest(StrictModel):
    token: str = Field(min_length=24, max_length=512)
    new_password: str = Field(min_length=12, max_length=256)


class KeyCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    allowed_models: list[str] | None = Field(default=None, min_length=1, max_length=100)
    max_output_tokens: int | None = Field(default=None, strict=True, ge=1, le=1_000_000)
    spend_limit_microusd: int | None = Field(default=None, strict=True, ge=1, le=10_000_000_000)

    @field_validator("allowed_models")
    @classmethod
    def unique_models(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (len(set(value)) != len(value) or any(not item or len(item) > 200 for item in value)):
            raise ValueError("模型列表必须非空且不重复")
        return value


class KeyRevokeRequest(StrictModel):
    key: str | None = Field(default=None, min_length=20, max_length=256)
    key_id: str | None = Field(default=None, min_length=4, max_length=32)

    @model_validator(mode="after")
    def one_identifier(self) -> "KeyRevokeRequest":
        if bool(self.key) == bool(self.key_id):
            raise ValueError("必须且只能提供 key 或 key_id")
        return self


class TopupRequest(StrictModel):
    amount: int = Field(ge=100, le=100_000_000, description="整数 microUSD 服务额度")


class LiveCheckoutRequest(StrictModel):
    sku: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    return_url: str | None = Field(default=None, min_length=8, max_length=2048)


class PaymentRefundRequest(StrictModel):
    reason: str = Field(min_length=10, max_length=500)
    idempotency_key: str = Field(
        min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


class PaymentReconcileRequest(StrictModel):
    reason: str = Field(min_length=10, max_length=500)


class RefundRiskDispositionRequest(StrictModel):
    action: Literal["recover_available", "write_off"]
    reason: str = Field(min_length=10, max_length=500)
    idempotency_key: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )


class AccountStatusRequest(StrictModel):
    action: Literal["freeze", "unfreeze"]
    reason: str = Field(min_length=10, max_length=500)
    expected_status: Literal["active", "frozen"] | None = None


class BudgetAmountRequest(StrictModel):
    amount: int = Field(gt=0, le=10_000_000_000)
    kind: Literal["prepaid_credit", "provider_spend_cap"] = "prepaid_credit"


class ProviderConnectionPutRequest(StrictModel):
    secret: SecretStr = Field(min_length=8, max_length=4096)
    label: str | None = Field(default=None, max_length=120)

    @field_validator("secret")
    @classmethod
    def secret_has_no_controls(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if any(ord(char) < 33 or ord(char) == 127 for char in raw):
            raise ValueError("密钥不能含控制字符或空白")
        return value


class ChatMessage(StrictModel):
    role: str = Field(pattern=r"^(system|developer|user|assistant|tool)$")
    content: str | list[dict[str, Any]] | None = None
    name: str | None = Field(default=None, max_length=128)
    tool_call_id: str | None = Field(default=None, max_length=256)
    tool_calls: list[dict[str, Any]] | None = None
    function_call: dict[str, Any] | None = None
    refusal: str | None = None


class ChatCompletionRequest(StrictModel):
    model: str = Field(min_length=1, max_length=120)
    messages: list[ChatMessage] = Field(min_length=1, max_length=256)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = None
    n: int = Field(default=1, ge=1, le=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    stop: str | list[str] | None = None
    user: str | None = Field(default=None, max_length=128)
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=128)
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None

    @model_validator(mode="after")
    def one_output_limit(self) -> "ChatCompletionRequest":
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("max_tokens 与 max_completion_tokens 不能同时设置")
        return self


class ReconciliationRequest(StrictModel):
    action: Literal["release", "settle"]
    reason: str = Field(min_length=12, max_length=500)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    upstream_cost_microusd: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def usage_required_for_settlement(self) -> "ReconciliationRequest":
        if self.action == "settle" and (
            self.input_tokens is None
            or self.output_tokens is None
            or self.upstream_cost_microusd is None
        ):
            raise ValueError("人工结算必须提供 Token 与已核对的上游成本")
        return self
