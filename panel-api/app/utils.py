from __future__ import annotations

import base64
import os


def gen_token(nbytes: int = 24) -> str:
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode().rstrip("=")

