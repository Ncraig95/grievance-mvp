from __future__ import annotations

import unittest

from grievance_api.web.routes_intake import _rewrite_signature_placeholders_for_stage


class StageAlignmentPlaceholderTests(unittest.TestCase):
    def test_stage2_keeps_only_signer2_and_remaps_to_signer1(self) -> None:
        src = (
            "A {{Sig_es_:signer1:signature}} "
            "B {{Dte_es_:signer2:date}} "
            "C {{Eml_es_:signer3:email}}"
        )
        out = _rewrite_signature_placeholders_for_stage(src, stage_no=2)
        self.assertNotIn("signer2", out.lower())
        self.assertNotIn("signer3", out.lower())
        self.assertIn("{{Dte_es_:signer1:date}}", out)
        self.assertNotIn("{{Sig_es_:signer1:signature}}", out)
        self.assertNotIn("{{Eml_es_:signer1:email}}", out)

    def test_stage3_keeps_only_signer3_and_remaps_to_signer1(self) -> None:
        src = (
            "{{Sig_es_:signer1:signature}}"
            "{{Sig_es_:signer2:signature}}"
            "{{Sig_es_:signer3:signature}}"
        )
        out = _rewrite_signature_placeholders_for_stage(src, stage_no=3)
        self.assertEqual(out.count("{{Sig_es_:signer1:signature}}"), 1)
        self.assertNotIn("signer2", out.lower())
        self.assertNotIn("signer3", out.lower())


if __name__ == "__main__":
    unittest.main()
