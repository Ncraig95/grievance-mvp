from __future__ import annotations

import unittest
from types import SimpleNamespace

from grievance_api.core.config import DocumentPolicyConfig
from grievance_api.services.staged_signature_workflow import (
    is_3g3a_staged,
    normalize_3g3a_signers,
    stage_key_for,
)


class StagedSignatureWorkflowTests(unittest.TestCase):
    def test_is_3g3a_staged_true_when_policy_enabled(self) -> None:
        cfg = SimpleNamespace(
            document_policies={
                "bst_grievance_form_3g3a": DocumentPolicyConfig(
                    folder_resolution="default",
                    default_signer_field="",
                    default_requires_signature=True,
                    staged_flow_enabled=True,
                    auto_advance=True,
                    store_all_stage_artifacts=True,
                    input_source="docuseal_fill_fields",
                )
            }
        )
        self.assertTrue(
            is_3g3a_staged(
                cfg=cfg,
                doc_type="bst_grievance_form_3g3a",
                template_key="bst_grievance_form_3g3a",
            )
        )

    def test_is_3g3a_staged_false_for_other_docs(self) -> None:
        cfg = SimpleNamespace(
            document_policies={
                "bst_grievance_form_3g3a": DocumentPolicyConfig(
                    folder_resolution="default",
                    default_signer_field="",
                    default_requires_signature=True,
                    staged_flow_enabled=True,
                    auto_advance=True,
                    store_all_stage_artifacts=True,
                    input_source="docuseal_fill_fields",
                )
            }
        )
        self.assertFalse(
            is_3g3a_staged(
                cfg=cfg,
                doc_type="statement_of_occurrence",
                template_key="statement_of_occurrence",
            )
        )

    def test_normalize_3g3a_signers_requires_three(self) -> None:
        self.assertEqual(normalize_3g3a_signers(["a@example.com", "b@example.com"]), [])
        self.assertEqual(
            normalize_3g3a_signers(["a@example.com", " b@example.com ", "c@example.com", "d@example.com"]),
            ["a@example.com", "b@example.com", "c@example.com"],
        )

    def test_stage_key_for(self) -> None:
        self.assertEqual(stage_key_for(1), "stage1_union")
        self.assertEqual(stage_key_for(2), "stage2_manager")
        self.assertEqual(stage_key_for(3), "stage3_union_final")


if __name__ == "__main__":
    unittest.main()
