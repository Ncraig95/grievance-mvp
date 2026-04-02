from __future__ import annotations

import unittest
from types import SimpleNamespace

from grievance_api.core.config import DocumentPolicyConfig
from grievance_api.services.staged_signature_workflow import (
    is_3g3a_staged,
    is_staged_document,
    normalize_3g3a_signers,
    normalize_staged_signers,
    resolve_staged_form_key,
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

    def test_is_staged_document_true_for_mobility_record_when_policy_enabled(self) -> None:
        cfg = SimpleNamespace(
            document_policies={
                "mobility_record_of_grievance": DocumentPolicyConfig(
                    folder_resolution="existing_exact_grievance_id",
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
            is_staged_document(
                cfg=cfg,
                doc_type="mobility_record_of_grievance",
                template_key="mobility_record_of_grievance",
            )
        )
        self.assertEqual(
            resolve_staged_form_key(
                cfg=cfg,
                doc_type="mobility_record_of_grievance",
                template_key="mobility_record_of_grievance",
            ),
            "mobility_record_of_grievance",
        )

    def test_normalize_3g3a_signers_requires_three(self) -> None:
        self.assertEqual(normalize_3g3a_signers(["a@example.com", "b@example.com"]), [])
        self.assertEqual(
            normalize_3g3a_signers(["a@example.com", " b@example.com ", "c@example.com", "d@example.com"]),
            ["a@example.com", "b@example.com", "c@example.com"],
        )

    def test_normalize_mobility_record_signers_requires_three(self) -> None:
        self.assertEqual(
            normalize_staged_signers(["a@example.com", "b@example.com"], form_key="mobility_record_of_grievance"),
            [],
        )
        self.assertEqual(
            normalize_staged_signers(
                ["a@example.com", " b@example.com ", "c@example.com", "d@example.com"],
                form_key="mobility_record_of_grievance",
            ),
            ["a@example.com", "b@example.com", "c@example.com"],
        )

    def test_stage_key_for(self) -> None:
        self.assertEqual(stage_key_for(1), "stage1_union")
        self.assertEqual(stage_key_for(2), "stage2_manager")
        self.assertEqual(stage_key_for(3), "stage3_union_final")
        self.assertEqual(stage_key_for(2, form_key="mobility_record_of_grievance"), "stage2_company")
        self.assertEqual(stage_key_for(3, form_key="mobility_record_of_grievance"), "stage3_union_appeal")


if __name__ == "__main__":
    unittest.main()
