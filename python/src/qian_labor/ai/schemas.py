import json
import math
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator


class SourceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str
    page: int | None = None
    row: int | None = None
    column: str | None = None
    sheet: str | None = None
    paragraph: int | None = None
    excerpt: str = ""
    bbox: tuple[float, float, float, float] | None = None


class EmploymentFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: str | None = None
    fact_type: str
    value: str | int | float | bool | list[Any] | dict[str, Any] | None
    confidence: float = Field(ge=0, le=1)
    source: SourceLocator
    needs_human_confirmation: bool = False


class UsageRecord(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: int = 0
    attempts: int = 1


class ExtractionResult(BaseModel):
    """Versioned provider-neutral extraction contract."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["employment-extraction-v1"] = "employment-extraction-v1"
    document_type: Literal[
        "roster",
        "contract",
        "attendance",
        "payroll",
        "social_insurance",
        "policy",
        "assessment",
        "termination",
        "notice",
        "other",
    ] = "other"
    employee_name: str | None = None
    employee_number: str | None = None
    id_number_hash: str | None = None
    phone_hash: str | None = None
    bank_card_hash: str | None = None
    department: str | None = None
    job_title: str | None = None
    dates: dict[str, str] = Field(default_factory=dict)
    probation: dict[str, Any] = Field(default_factory=dict)
    wages: dict[str, Any] = Field(default_factory=dict)
    attendance: dict[str, Any] = Field(default_factory=dict)
    overtime: dict[str, Any] = Field(default_factory=dict)
    social_insurance: dict[str, Any] = Field(default_factory=dict)
    termination: dict[str, Any] = Field(default_factory=dict)
    notice_and_delivery: dict[str, Any] = Field(default_factory=dict)
    assessment_materials: list[str] = Field(default_factory=list)
    entities: dict[str, str] = Field(default_factory=dict)
    needs_human_confirmation: bool = False
    facts: list[EmploymentFact] = Field(default_factory=list)
    usage: UsageRecord = Field(default_factory=UsageRecord)


class ProviderSource(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    file_name: str
    page: int | None
    row: int | None
    column: str | None
    sheet: str | None
    paragraph: int | None
    excerpt: str
    bbox: list[FiniteFloat] | None

    @field_validator("bbox")
    @classmethod
    def validate_bbox_length(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and len(value) != 4:
            raise ValueError("PROVIDER_SOURCE_BBOX_INVALID")
        return value

    def to_source_locator(self) -> SourceLocator:
        payload = self.model_dump(exclude={"bbox"})
        bbox = (
            (self.bbox[0], self.bbox[1], self.bbox[2], self.bbox[3])
            if self.bbox is not None
            else None
        )
        return SourceLocator(
            **payload,
            bbox=bbox,
        )


ProviderValueType = Literal[
    "null",
    "text",
    "integer",
    "number",
    "boolean",
    "string_list",
    "json",
]


class ProviderFact(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    employee_id: str | None
    fact_type: str
    value_type: ProviderValueType
    value_text: str | None
    value_integer: int | None
    value_number: FiniteFloat | None
    value_boolean: bool | None
    value_string_list: list[str] | None
    value_json: str | None
    confidence: FiniteFloat = Field(ge=0, le=1)
    source: ProviderSource
    needs_human_confirmation: bool

    def to_employment_fact(self) -> EmploymentFact:
        values: dict[str, object | None] = {
            "null": None,
            "text": self.value_text,
            "integer": self.value_integer,
            "number": self.value_number,
            "boolean": self.value_boolean,
            "string_list": self.value_string_list,
            "json": self._parse_limited_json(),
        }
        selected = values[self.value_type]
        if self.value_type != "null" and selected is None:
            raise ValueError("PROVIDER_FACT_VALUE_MISSING")
        field_for_type = {
            "text": "value_text",
            "integer": "value_integer",
            "number": "value_number",
            "boolean": "value_boolean",
            "string_list": "value_string_list",
            "json": "value_json",
        }.get(self.value_type)
        for field in (
            "value_text",
            "value_integer",
            "value_number",
            "value_boolean",
            "value_string_list",
            "value_json",
        ):
            if field != field_for_type and getattr(self, field) is not None:
                raise ValueError("PROVIDER_FACT_VALUE_AMBIGUOUS")
        return EmploymentFact(
            employee_id=self.employee_id,
            fact_type=self.fact_type,
            value=cast(str | int | float | bool | list[Any] | dict[str, Any] | None, selected),
            confidence=self.confidence,
            source=self.source.to_source_locator(),
            needs_human_confirmation=self.needs_human_confirmation,
        )

    def _parse_limited_json(self) -> object | None:
        if self.value_type != "json":
            return None
        if self.value_json is None or len(self.value_json.encode("utf-8")) > 16_384:
            raise ValueError("PROVIDER_FACT_JSON_INVALID")
        value = json.loads(
            self.value_json,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("INVALID_CONSTANT")),
        )
        self._validate_json_tree(value, depth=0, remaining=[1_000])
        if not isinstance(value, (dict, list)):
            raise ValueError("PROVIDER_FACT_JSON_INVALID")
        return value

    @classmethod
    def _validate_json_tree(cls, value: object, *, depth: int, remaining: list[int]) -> None:
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 8:
            raise ValueError("PROVIDER_FACT_JSON_LIMIT")
        if isinstance(value, dict):
            if len(value) > 100 or not all(isinstance(key, str) for key in value):
                raise ValueError("PROVIDER_FACT_JSON_LIMIT")
            for item in value.values():
                cls._validate_json_tree(item, depth=depth + 1, remaining=remaining)
        elif isinstance(value, list):
            if len(value) > 100:
                raise ValueError("PROVIDER_FACT_JSON_LIMIT")
            for item in value:
                cls._validate_json_tree(item, depth=depth + 1, remaining=remaining)
        elif isinstance(value, str):
            if len(value) > 4_096:
                raise ValueError("PROVIDER_FACT_JSON_LIMIT")
        elif (
            isinstance(value, float)
            and not math.isfinite(value)
            or value is not None
            and not isinstance(value, (str, int, float, bool))
        ):
            raise ValueError("PROVIDER_FACT_JSON_LIMIT")


class ProviderExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal["employment-extraction-v1"]
    document_type: Literal[
        "roster",
        "contract",
        "attendance",
        "payroll",
        "social_insurance",
        "policy",
        "assessment",
        "termination",
        "notice",
        "other",
    ]
    employee_name: str | None
    employee_number: str | None
    department: str | None
    job_title: str | None
    needs_human_confirmation: bool
    facts: list[ProviderFact]

    def to_extraction_result(self) -> ExtractionResult:
        return ExtractionResult(
            schema_version=self.schema_version,
            document_type=self.document_type,
            employee_name=self.employee_name,
            employee_number=self.employee_number,
            department=self.department,
            job_title=self.job_title,
            needs_human_confirmation=self.needs_human_confirmation,
            facts=[item.to_employment_fact() for item in self.facts],
        )
