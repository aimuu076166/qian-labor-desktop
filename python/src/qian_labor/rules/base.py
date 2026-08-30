from collections.abc import Callable
from dataclasses import dataclass

from qian_labor.rules.types import RuleContext, RuleMetadata, RuleResult

Evaluator = Callable[[RuleContext, RuleMetadata], RuleResult]
Applicability = Callable[[RuleContext], bool | None]


@dataclass(frozen=True)
class RiskRule:
    metadata: RuleMetadata
    evaluator: Evaluator
    applicability: Applicability | None = None

    def evaluate(self, context: RuleContext) -> RuleResult:
        if self.applicability is not None and self.applicability(context) is False:
            return RuleResult(
                rule_id=self.metadata.rule_id,
                rule_version=self.metadata.version,
                triggered=False,
                assessment_status="not_triggered",
                severity=self.metadata.severity,
                message_params={"finding_phrase": "当前人员状态不适用本规则"},
            )
        available = [
            context.facts[name] for name in self.metadata.required_facts if name in context.facts
        ]
        missing = tuple(name for name in self.metadata.required_facts if name not in context.facts)
        sources = tuple(
            dict.fromkeys(source for fact in available for source in fact.source_locator_ids)
        )
        fact_ids = tuple(fact.id for fact in available)
        if missing:
            return RuleResult(
                rule_id=self.metadata.rule_id,
                rule_version=self.metadata.version,
                triggered=True,
                assessment_status="insufficient_data",
                severity="info",
                trigger_fact_ids=fact_ids,
                source_locator_ids=sources,
                missing_fact_types=missing,
                message_params={"finding_phrase": "资料不足，暂时无法判断，请补充或核对材料"},
                requires_human_review=self.metadata.requires_human_review,
            )
        if any(fact.conflicted for fact in available):
            return RuleResult(
                rule_id=self.metadata.rule_id,
                rule_version=self.metadata.version,
                triggered=True,
                assessment_status="requires_human_review",
                severity=self.metadata.severity,
                trigger_fact_ids=fact_ids,
                source_locator_ids=sources,
                message_params={"finding_phrase": "材料之间存在冲突，需要人工复核"},
                requires_human_review=True,
            )
        return self.evaluator(context, self.metadata)
