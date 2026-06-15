"""Structured output schemas for the meal_planner agents (Gemini via ADK)."""

from pydantic import BaseModel, Field


class RankingOutput(BaseModel):
    """The ranker's entire job: preference order + set-coherence judgment.
    No numbers, no totals — those are code's."""

    ranking: list[str] = Field(
        description="Every recipe_id from the pool, most-wanted first for this household this week."
    )
    pairing_cautions: list[list[str]] = Field(
        default_factory=list,
        description="Pairs of recipe_ids that would feel repetitive/samey if both appeared in the same week.",
    )
    rationales: dict[str, str] = Field(
        default_factory=dict,
        description="recipe_id -> one short user-facing reason for the top ~10 picks.",
    )


class FeatureProposal(BaseModel):
    feature_key: str = Field(description="One of the known user-model feature keys.")
    delta: float = Field(description="Proposed adjustment; code clamps it to a safe bound.")
    evidence: str = Field(description="The behavioral evidence for this proposal.")


class ReflectionOutput(BaseModel):
    """Weekly reflection: rewrite the narrative; numbers only as bounded proposals."""

    narrative: str = Field(
        description="3-5 short observations about this household's real behavior. No numbers, no constraints."
    )
    proposals: list[FeatureProposal] = Field(default_factory=list)
