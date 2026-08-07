"""Diff review JSON-RPC method handlers for GatewaySession."""

from __future__ import annotations

import uuid

from voidx.presentation.gateway.diff_review import DiffReviewSession
from voidx.presentation.protocol.v2.methods import MethodParamsError


class DiffMethods:
    """Diff-review-related JSON-RPC handlers, mixed into GatewaySession."""

    def _method_diff_review_start(self, params: dict) -> dict:
        diff_text = params.get("diff", "")
        if not diff_text:
            raise MethodParamsError("diff is required")
        review_id = uuid.uuid4().hex[:12]
        review = DiffReviewSession.from_diff(diff_text)
        self._diff_reviews[review_id] = review
        return {"review_id": review_id, "snapshot": review.to_snapshot()}

    def _method_diff_review_decide(self, params: dict) -> dict:
        review_id = params.get("review_id", "")
        review = self._diff_reviews.get(review_id)
        if review is None:
            raise MethodParamsError(f"review not found: {review_id}")
        file_path = params.get("file_path", "")
        hunk_index = params.get("hunk_index", -1)
        decision = params.get("decision", "")
        review.decide(file_path, hunk_index, decision)
        return {"summary": review.summary()}

    def _method_diff_review_apply(self, params: dict) -> dict:
        review_id = params.get("review_id", "")
        review = self._diff_reviews.get(review_id)
        if review is None:
            raise MethodParamsError(f"review not found: {review_id}")
        changed = review.apply()
        return {"files_changed": changed}

    def _method_diff_generate(self, params: dict) -> dict:
        import subprocess

        cwd = self._workspace or None
        try:
            result = subprocess.run(
                ["git", "diff", "--unified=3"],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=10,
            )
            return {"diff": result.stdout.strip()}
        except Exception:
            return {"diff": ""}
