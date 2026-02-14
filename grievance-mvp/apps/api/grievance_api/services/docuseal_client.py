from __future__ import annotations

import requests

class DocuSealClient:
    """
    Thin wrapper with placeholders.
    You must fill in endpoints and payload shapes based on DocuSeal official API docs.
    """

    def __init__(self, base_url: str, api_token: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def create_submission(self, *, pdf_bytes: bytes, signer_email: str, signer_name: str, title: str) -> dict:
        """
        TODO: Implement per DocuSeal docs.
        Return:
          - submission_id
          - signing_link (optional)
        """
        raise NotImplementedError("Fill in DocuSeal create_submission per official docs")

    def download_completed_artifacts(self, *, submission_id: str) -> dict:
        """
        TODO: Implement per DocuSeal docs.
        Return:
          - signed_pdf_bytes
          - certificate_bytes_or_json
        """
        raise NotImplementedError("Fill in DocuSeal download_completed_artifacts per official docs")
