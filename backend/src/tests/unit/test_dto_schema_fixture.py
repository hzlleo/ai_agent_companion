"""DTO Schema fixture 新鲜度测试——后端 DTO 改动未重新导出即红。

生成逻辑在仓库根 scripts/export_dto_schemas.py（经 src/conftest.py 加入
sys.path，按顶层模块名导入——与 ``pixi run export-dto-schemas`` 执行口径
一致）；fixture 为已提交的 frontend/src/test/contract/dtoSchemas.json
（绝对路径由脚本内锚点推导，不受测试 scratch CWD 影响）。

门禁分工：本测试拦「后端 DTO 改动未重新导出」方向；前端
src/test/contract/dtoContract.test.ts 拦「前端手写 types 漂移」方向。
"""

from __future__ import annotations

from export_dto_schemas import DTO_SCHEMAS_FIXTURE_PATH, export_fixture


async def test_dto_schema_fixture_freshness() -> None:
    """内存再生内容与提交的前端 fixture 逐字节一致（byte-stable 契约）。"""
    regenerated = export_fixture(write=False)
    assert DTO_SCHEMAS_FIXTURE_PATH.is_file(), (
        "提交的 fixture 缺失: frontend/src/test/contract/dtoSchemas.json"
    )
    committed = DTO_SCHEMAS_FIXTURE_PATH.read_text(encoding="utf-8")
    assert regenerated == committed, (
        "后端 DTO 与前端契约 fixture 漂移——后端 DTO 改动后须执行 "
        "`cd backend && pixi run export-dto-schemas` 再提交新 fixture"
    )


async def test_dto_schema_fixture_deterministic() -> None:
    """连续两次再生结果一致（无随机性 / 无环境值渗入）。"""
    first = export_fixture(write=False)
    second = export_fixture(write=False)
    assert first == second
