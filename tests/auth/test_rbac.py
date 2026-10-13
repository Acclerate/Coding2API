"""RBAC 鉴权。"""

from __future__ import annotations

import pytest

from src.auth.rbac import ForbiddenError, Principal, require_admin


def test_require_admin():
    assert require_admin(Principal("root", True)).username == "root"
    with pytest.raises(ForbiddenError):
        require_admin(Principal("u", False))
