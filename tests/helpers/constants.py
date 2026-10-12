"""测试通用常量。"""

from __future__ import annotations

# 测试用 APP_SECRET：必须满足 CredentialCipher 的最短长度校验（>=16）
SECRET = "test-secret-0123456789"

# 三用户测试集：由 conftest 的 users_file_path 写成一个临时 users.txt
USERS = (("root", "rootpw"), ("guest", "guestpw"), ("alice", "alicepw"))
