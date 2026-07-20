-- Phase 7A extension: distinguish candidate-context packages from full-document packages.

ALTER TABLE mofa_research_packages
    ADD COLUMN selection_scope TEXT NOT NULL DEFAULT 'candidate_context'
        CHECK(selection_scope IN ('candidate_context', 'full_document'));
