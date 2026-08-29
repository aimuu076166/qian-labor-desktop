from enum import StrEnum


class AnalysisStatus(StrEnum):
    CREATED = "created"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PARSING = "parsing"
    EXTRACTING = "extracting"
    MATCHING_REVIEW = "matching_review"
    REVIEW_REQUIRED = "matching_review"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


ALLOWED_TRANSITIONS = {
    AnalysisStatus.CREATED: {
        AnalysisStatus.UPLOADING,
        AnalysisStatus.UPLOADED,
        AnalysisStatus.DELETING,
    },
    AnalysisStatus.UPLOADING: {AnalysisStatus.UPLOADED, AnalysisStatus.DELETING},
    AnalysisStatus.UPLOADED: {AnalysisStatus.QUEUED, AnalysisStatus.DELETING},
    AnalysisStatus.QUEUED: {AnalysisStatus.PARSING, AnalysisStatus.DELETING},
    AnalysisStatus.PARSING: {
        AnalysisStatus.EXTRACTING,
        AnalysisStatus.FAILED,
        AnalysisStatus.DELETING,
    },
    AnalysisStatus.EXTRACTING: {
        AnalysisStatus.MATCHING_REVIEW,
        AnalysisStatus.EVALUATING,
        AnalysisStatus.FAILED,
        AnalysisStatus.DELETING,
    },
    AnalysisStatus.MATCHING_REVIEW: {AnalysisStatus.EVALUATING, AnalysisStatus.DELETING},
    AnalysisStatus.EVALUATING: {
        AnalysisStatus.COMPLETED,
        AnalysisStatus.PARTIAL,
        AnalysisStatus.FAILED,
        AnalysisStatus.DELETING,
    },
    AnalysisStatus.COMPLETED: {AnalysisStatus.DELETING},
    AnalysisStatus.PARTIAL: {AnalysisStatus.QUEUED, AnalysisStatus.DELETING},
    AnalysisStatus.FAILED: {AnalysisStatus.QUEUED, AnalysisStatus.DELETING},
    AnalysisStatus.DELETING: {AnalysisStatus.DELETED},
    AnalysisStatus.DELETED: set(),
}
