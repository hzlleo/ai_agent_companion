"""容器入口 VNC 密码解析的 hermetic 测试。

直接 source ``entrypoint.sh`` 后调用 ``resolve_vnc_passfile``（入口主体由
``BASH_SOURCE`` 自执行守卫隔开），因此不开 Xorg、不建容器、不触碰真实
``/run/secrets``。判定「运行用户可读」经 setpriv 降到目标 uid，root 的
``test -r`` 恒真这一口径缺陷由用例钉住。
"""

from __future__ import annotations

import asyncio
import os
import pwd
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[3] / "entrypoint.sh"
_RUN_USER = pwd.getpwuid(os.getuid()).pw_name


async def _resolve(
    *,
    source: Path,
    runtime: Path,
    password: str | None = None,
    probe_body: str | None = None,
) -> tuple[int, str, str]:
    """在子 shell 中调用入口脚本的密码解析函数。

    Args:
        source: 密码文件路径。
        runtime: 运行目录。
        password: 明文密码（None 表示不设 ``WECHAT_GUI_VNC_PASSWORD``）。
        probe_body: 注入的探针函数定义（None 表示用入口的默认 setpriv 探针）。

    Returns:
        (退出码, 标准输出, 标准错误)。
    """
    env = {key: value for key, value in os.environ.items() if key != "WECHAT_GUI_VNC_PASSWORD"}
    if password is not None:
        env["WECHAT_GUI_VNC_PASSWORD"] = password
    calls = 'resolve_vnc_passfile "$2" "$3" "$4"'
    prelude = ""
    if probe_body is not None:
        prelude = f"{probe_body}; "
        calls = 'resolve_vnc_passfile "$2" "$3" "$4" masked_probe'
    script = f'source "$1" || exit 99; {prelude}{calls}'
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        script,
        "entrypoint",
        str(ENTRYPOINT),
        str(source),
        _RUN_USER,
        str(runtime),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode().strip(), stderr.decode()


async def test_resolve_keeps_file_readable_by_run_user(tmp_path: Path) -> None:
    """运行用户可读的密码文件原样使用。"""
    source = tmp_path / "vnc.pass"
    source.write_text("secret\n", encoding="utf-8")
    source.chmod(0o644)
    code, stdout, _stderr = await _resolve(source=source, runtime=tmp_path / "run")
    assert code == 0
    assert stdout == str(source)


async def test_resolve_falls_back_to_env_password(tmp_path: Path) -> None:
    """密码文件不可用时，明文密码写入运行目录（0400，运行用户所有）。"""
    runtime = tmp_path / "run"
    runtime.mkdir()
    code, stdout, _stderr = await _resolve(
        source=tmp_path / "missing.pass",
        runtime=runtime,
        password="hunter2",
    )
    assert code == 0
    target = runtime / "vnc.pass"
    assert stdout == str(target)
    assert target.read_text(encoding="utf-8") == "hunter2\n"
    stat_result = target.stat()
    assert stat_result.st_mode & 0o777 == 0o400
    assert stat_result.st_uid == os.getuid()


async def test_resolve_prefers_env_password_over_file(tmp_path: Path) -> None:
    """显式明文密码优先于同时存在的密码文件。"""
    runtime = tmp_path / "run"
    runtime.mkdir()
    source = tmp_path / "vnc.pass"
    source.write_text("from-file\n", encoding="utf-8")
    source.chmod(0o644)
    code, stdout, _stderr = await _resolve(
        source=source,
        runtime=runtime,
        password="from-env",
    )
    assert code == 0
    assert stdout == str(runtime / "vnc.pass")
    assert (runtime / "vnc.pass").read_text(encoding="utf-8") == "from-env\n"


async def test_resolve_fails_when_nobody_can_read(tmp_path: Path) -> None:
    """文件对运行用户与 root 都不可读且无明文密码时失败（不静默降级）。"""
    source = tmp_path / "vnc.pass"
    source.write_text("secret\n", encoding="utf-8")
    source.chmod(0o000)
    code, stdout, _stderr = await _resolve(source=source, runtime=tmp_path / "run")
    assert code != 0
    assert stdout == ""


async def test_resolve_probe_distinguishes_run_user_from_caller(tmp_path: Path) -> None:
    """调用方可读而运行用户不可读时走复制，钉住探针不可退化为 test -r。

    非 root 单测无法真降权到第二个 uid；把「运行用户是否可读」的探针做成
    可注入的函数入参后，用只拒绝源文件的替身构造该场景：若实现改用调用方
    口径的 ``[[ -r ]]``，会直接复用源文件（stdout 为 source）而红。
    """
    source = tmp_path / "vnc.pass"
    source.write_text("secret\n", encoding="utf-8")
    source.chmod(0o400)
    runtime = tmp_path / "run"
    runtime.mkdir()
    probe_body = f'masked_probe() {{ [[ "$1" != "{source}" ]]; }}'
    code, stdout, _stderr = await _resolve(
        source=source,
        runtime=runtime,
        probe_body=probe_body,
    )
    assert code == 0
    target = runtime / "vnc.pass"
    assert stdout == str(target)
    assert target.read_text(encoding="utf-8") == "secret\n"
    stat_result = target.stat()
    assert stat_result.st_mode & 0o777 == 0o400
    assert stat_result.st_uid == os.getuid()


async def _resolve_agent_token(
    *, source: Path, runtime: Path, probe_body: str | None = None
) -> tuple[int, str, str]:
    """在子 shell 中调用入口脚本的 agent token 解析函数。

    Args:
        source: token 文件路径。
        runtime: 运行目录。
        probe_body: 注入的探针函数定义（None 表示用入口的默认 setpriv 探针）。

    Returns:
        (退出码, 标准输出, 标准错误)。
    """
    calls = 'resolve_agent_token_file "$2" "$3" "$4"'
    prelude = ""
    if probe_body is not None:
        prelude = f"{probe_body}; "
        calls = 'resolve_agent_token_file "$2" "$3" "$4" masked_probe'
    script = f'source "$1" || exit 99; {prelude}{calls}'
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        script,
        "entrypoint",
        str(ENTRYPOINT),
        str(source),
        _RUN_USER,
        str(runtime),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode().strip(), stderr.decode()


async def test_agent_token_keeps_readable_file_and_copies_root_only(tmp_path: Path) -> None:
    """agent token：运行用户可读原样使用；仅 root 可读时复制到运行目录。"""
    readable = tmp_path / "agent.token"
    readable.write_text("token\n", encoding="utf-8")
    readable.chmod(0o644)
    code, stdout, _stderr = await _resolve_agent_token(
        source=readable, runtime=tmp_path / "run-readable"
    )
    assert code == 0
    assert stdout == str(readable)

    runtime = tmp_path / "run-copy"
    runtime.mkdir()
    source = tmp_path / "copied.token"
    source.write_text("token\n", encoding="utf-8")
    source.chmod(0o600)
    code, stdout, _stderr = await _resolve_agent_token(
        source=source,
        runtime=runtime,
        probe_body="masked_probe() { return 1; }",
    )
    assert code == 0
    copied = runtime / "agent.token"
    assert stdout == str(copied)
    assert copied.read_text(encoding="utf-8") == "token\n"


async def test_agent_token_missing_file_fails(tmp_path: Path) -> None:
    """agent token：两处都不可读时解析失败（入口拒绝启动）。"""
    code, stdout, _stderr = await _resolve_agent_token(
        source=tmp_path / "missing.token", runtime=tmp_path / "run"
    )
    assert code == 1
    assert stdout == ""
