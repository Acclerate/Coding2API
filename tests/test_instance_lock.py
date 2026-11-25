"""单实例锁：同一 DATA_DIR 只能有一个进程。

背景：SQLite 允许多进程并发写（WAL + 忙等），所以「起了两个实例」不会报错，
只会安静地互相覆盖——两个进程各自跑后台任务（额度探测、签到、模型目录刷新），
同一凭证被探两次、同一小时汇总写两遍。更常见的形态是 launchd 崩溃循环：
端口被占 → 绑定失败 → 退出 → KeepAlive 每 10s 重拉（实测刷了 2377 次
`[Errno 48]`，见 2026-09-19~10-17 的 logs/launchd.err.log）。

锁用 `flock` 而不是端口探测：端口探测有 TOCTOU 窗口，且只能保护「同端口」
的实例；两个实例配不同 PORT 时照样双写同一个库。flock 由内核在进程退出
（含 SIGKILL）时自动释放，不留需要人工清理的陈旧锁。
"""

from __future__ import annotations

import os

import pytest

from src.instance_lock import LOCK_NAME, AlreadyRunning, InstanceLock, hold_instance_lock


def test_acquire_creates_lock_file_with_pid(tmp_path):
    """拿到锁后锁文件里写着本进程 PID（仅用于报错文案）。"""
    lock = InstanceLock(tmp_path)
    lock.acquire()
    try:
        assert (tmp_path / LOCK_NAME).read_text(encoding="utf-8").strip() == str(
            os.getpid())
    finally:
        lock.release()


def test_second_acquire_in_same_process_is_rejected(tmp_path):
    """同进程内第二次取锁也失败：flock 是进程级的，重入不递增计数。"""
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            InstanceLock(tmp_path).acquire()
    finally:
        first.release()


def test_second_process_is_rejected(tmp_path):
    """真正的并发场景：子进程持锁期间，父进程取不到。"""
    import subprocess
    import sys
    import textwrap

    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(os.getcwd())!r})
            from src.instance_lock import InstanceLock
            lock = InstanceLock({str(tmp_path)!r})
            lock.acquire()
            print("locked", flush=True)
            time.sleep(30)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(AlreadyRunning) as excinfo:
            InstanceLock(tmp_path).acquire()
        # 报错文案必须带持有者 PID：崩溃循环里这行是唯一线索
        assert "PID" in str(excinfo.value)
        assert excinfo.value.holder.strip() != str(os.getpid())
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_release_allows_reacquire(tmp_path):
    """释放后可以再取（重启场景）。"""
    lock = InstanceLock(tmp_path)
    lock.acquire()
    lock.release()
    again = InstanceLock(tmp_path)
    again.acquire()
    again.release()


def test_release_is_idempotent(tmp_path):
    """重复 release 不抛：finally 里可能被调用两次。"""
    lock = InstanceLock(tmp_path)
    lock.acquire()
    lock.release()
    lock.release()


def test_context_manager_releases_on_exception(tmp_path):
    """异常路径也要释放，否则下一个实例永远起不来。"""
    with pytest.raises(ValueError), InstanceLock(tmp_path):
        raise ValueError("boom")
    InstanceLock(tmp_path).acquire()


def test_missing_lock_file_reports_unknown_holder(tmp_path):
    """锁文件读不到时 holder 回落 unknown，不让报错路径自己崩掉。"""
    lock = InstanceLock(tmp_path)
    assert lock._read_holder() == "unknown"


def test_empty_lock_file_reports_unknown_holder(tmp_path):
    """空内容同样回落 unknown（被 truncate 但还没写 PID 的瞬间）。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / LOCK_NAME).write_text("   \n", encoding="utf-8")
    assert InstanceLock(tmp_path)._read_holder() == "unknown"


def test_hold_instance_lock_creates_data_dir(tmp_path):
    """DATA_DIR 还不存在时也能取锁（首次启动）。"""
    target = tmp_path / "fresh"
    lock = hold_instance_lock(target)
    try:
        assert (target / LOCK_NAME).is_file()
    finally:
        lock.release()


def test_already_running_message_is_actionable(tmp_path):
    """报错文案必须说清「同目录只能一个实例」与怎么解除。"""
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning) as excinfo:
            InstanceLock(tmp_path).acquire()
        text = str(excinfo.value)
        assert "另一个实例" in text
        assert "只能起一个进程" in text
        assert "删掉" in text
    finally:
        first.release()


# ------------------------------------------------------- 平台分支（issue #46）


def test_module_is_importable_without_fcntl(monkeypatch):
    """`fcntl` 与 `msvcrt` 都缺失时模块仍可导入，但 `LOCK_SUPPORTED` 为假。

    模块级 import 失败会让 `build_app --factory` 与所有 `scripts/*` 直接炸掉
    （连一句自己的错误都打不出来），所以导入必须宽容。fork 给 Windows 补了
    msvcrt 锁，所以这里把 `sys.platform` 一并改成非 win32，模拟「两个原语
    都不存在」的第三方平台。

    用 `module_from_spec` 在**独立命名空间**执行模块代码，而不是 reload 真
    模块：reload 会把模块里的类对象整体换成新类，而其他模块（含本测试文件）
    早已 `from ... import` 了旧类——此后所有 isinstance / pytest.raises 都
    会静默失配（真实踩坑：reload 后追加的用例接不住 AlreadyRunning）。
    """
    import importlib.util
    import sys

    import src.instance_lock as real_module

    monkeypatch.setitem(sys.modules, "fcntl", None)  # import fcntl 抛 ImportError
    monkeypatch.setattr(sys, "platform", "cygwin")  # 走「无 msvcrt」分支
    spec = importlib.util.find_spec("src.instance_lock")
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    # 真模块必须原封不动：其他地方的旧引用继续有效
    assert real_module.LOCK_SUPPORTED is True
    assert isolated.LOCK_SUPPORTED is False
    assert isolated.fcntl is None and isolated.msvcrt is None


def test_acquire_refuses_to_silently_noop_without_fcntl(monkeypatch, tmp_path):
    """不支持时**必须抛错**，绝不能 no-op 偷跑：后者等于静默放弃单实例保护。"""
    from src import instance_lock

    monkeypatch.setattr(instance_lock, "LOCK_SUPPORTED", False)
    with pytest.raises(instance_lock.LockUnsupported) as excinfo:
        instance_lock.InstanceLock(tmp_path).acquire()
    text = str(excinfo.value)
    assert "不支持文件锁" in text
    assert "WSL2" in text or "Docker" in text
    # 抛错前不该留下锁文件（否则下次会看到一个空的陈旧锁）
    assert not (tmp_path / LOCK_NAME).exists()


def test_hold_instance_lock_propagates_unsupported(monkeypatch, tmp_path):
    """`hold_instance_lock` 不吞异常：调用方需要拿到它决定退出码。"""
    from src import instance_lock

    monkeypatch.setattr(instance_lock, "LOCK_SUPPORTED", False)
    with pytest.raises(instance_lock.LockUnsupported):
        instance_lock.hold_instance_lock(tmp_path)


# ------------------------------------------------- Windows 分支（fork 补齐）


class FakeMsvcrt:
    """`msvcrt.locking` 的替身：按文件（st_dev, st_ino）记录锁状态。

    真模块的区域锁落在**文件**上——同一文件的两个句柄必然冲突，与 fd 无关；
    按 fd 记键会漏掉这个冲突。真 `msvcrt` 只在 Windows 存在；CI（Linux）靠
    这个替身覆盖 Windows 分支，fork 的 Windows 部署机另有原生用例（上面
    不走替身的用例在 win32 上自动落到真 `msvcrt` 路径）。
    """

    LK_UNLCK = 0
    LK_NBLCK = 2

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self.locked: set[tuple[int, int]] = set()

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((fd, mode, nbytes))
        stat_result = os.fstat(fd)
        key = (stat_result.st_dev, stat_result.st_ino)
        if mode == self.LK_UNLCK:
            self.locked.discard(key)
            return
        if key in self.locked:  # LK_NBLCK：区域已被锁，立即失败（真模块抛 OSError）
            raise OSError(36, "Resource deadlock avoided")
        self.locked.add(key)


@pytest.fixture()
def fake_msvcrt(monkeypatch):
    from src import instance_lock

    fake = FakeMsvcrt()
    monkeypatch.setattr(instance_lock, "msvcrt", fake)
    return fake


def test_windows_branch_acquires_region_lock(fake_msvcrt, tmp_path):
    """Windows 路径：锁第 0 字节 1 字节长、锁文件写 PID、解锁在 release。"""
    lock = InstanceLock(tmp_path)
    lock.acquire()
    try:
        assert fake_msvcrt.calls[0][1] == FakeMsvcrt.LK_NBLCK
        assert fake_msvcrt.calls[0][2] == 1
        assert (tmp_path / LOCK_NAME).read_text(encoding="utf-8").strip() == str(
            os.getpid())
    finally:
        lock.release()
    assert fake_msvcrt.calls[-1][1] == FakeMsvcrt.LK_UNLCK


def test_windows_branch_second_lock_is_already_running(fake_msvcrt, tmp_path):
    """区域锁已被占用 → OSError 归一成 AlreadyRunning（与 flock 同语义）。"""
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning) as excinfo:
            InstanceLock(tmp_path).acquire()
        assert "另一个实例" in str(excinfo.value)
    finally:
        first.release()


def test_windows_branch_release_allows_reacquire(fake_msvcrt, tmp_path):
    """解锁后能再取：重启场景下锁不残留。"""
    lock = InstanceLock(tmp_path)
    lock.acquire()
    lock.release()
    again = InstanceLock(tmp_path)
    again.acquire()
    again.release()
    # 锁过两次、解锁两次
    assert sum(mode == FakeMsvcrt.LK_NBLCK for _, mode, _ in fake_msvcrt.calls) == 2
    assert sum(mode == FakeMsvcrt.LK_UNLCK for _, mode, _ in fake_msvcrt.calls) == 2
