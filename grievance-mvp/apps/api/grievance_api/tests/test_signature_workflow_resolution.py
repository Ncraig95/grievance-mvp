from __future__ import annotations

from types import SimpleNamespace
import unittest

from grievance_api.services.signature_workflow import resolve_docuseal_template_id, resolve_form_key


class SignatureWorkflowResolutionTests(unittest.TestCase):
    def test_resolve_form_key_prefers_template_key(self) -> None:
        self.assertEqual(
            resolve_form_key(template_key="settlement_form_3106", doc_type="settlement_form"),
            "settlement_form_3106",
        )

    def test_resolve_docuseal_template_id_prefers_template_key_mapping(self) -> None:
        cfg = SimpleNamespace(
            docuseal=SimpleNamespace(
                template_ids={"settlement_form_3106": 44, "settlement_form": 33},
                default_template_id=2,
                strict_template_ids=False,
            )
        )
        result = resolve_docuseal_template_id(
            cfg,
            template_key="settlement_form_3106",
            doc_type="settlement_form",
        )
        self.assertEqual(result, 44)

    def test_resolve_docuseal_template_id_uses_default_when_not_strict(self) -> None:
        cfg = SimpleNamespace(
            docuseal=SimpleNamespace(
                template_ids={},
                default_template_id=2,
                strict_template_ids=False,
            )
        )
        result = resolve_docuseal_template_id(
            cfg,
            template_key="unknown_form",
            doc_type="unknown_form",
        )
        self.assertEqual(result, 2)

    def test_resolve_docuseal_template_id_raises_when_strict_and_missing(self) -> None:
        cfg = SimpleNamespace(
            docuseal=SimpleNamespace(
                template_ids={},
                default_template_id=2,
                strict_template_ids=True,
            )
        )
        with self.assertRaises(RuntimeError):
            resolve_docuseal_template_id(
                cfg,
                template_key="settlement_form_3106",
                doc_type="settlement_form_3106",
            )


if __name__ == "__main__":
    unittest.main()
