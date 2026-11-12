"""``core.dtos`` 单元测试——ResultDTO / PageResponse / 游标编解码。

覆盖矩阵：

    - ResultDTO：ok / error 构造、默认值、JSON 序列化形态（契约锁定）；
    - PageResponse：offset 模式 ``of`` / cursor 模式 ``of_cursor``、
      has_more 推导、字段默认值与校验约束；
    - 游标：encode/decode 往返、空 payload、malformed 输入容错。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from packages.harness.core.dtos import (
    PageResponse,
    ResultDTO,
    decode_cursor,
    encode_cursor,
)


class TestResultDTO:
    async def test_ok_defaults(self) -> None:
        """ok 构造：success=True / code=200 / message=成功。"""
        dto = ResultDTO.ok()
        assert dto.success is True
        assert dto.code == "200"
        assert dto.message == "成功"
        assert dto.data is None

    async def test_ok_with_data_and_message(self) -> None:
        """ok 构造携带 data 与自定义 message。"""
        dto = ResultDTO.ok(data={"id": 1}, message="已创建")
        assert dto.data == {"id": 1}
        assert dto.message == "已创建"

    async def test_error_shape(self) -> None:
        """error 构造：success=False / data=None / code+message 透传。"""
        dto = ResultDTO.error(message="人格不存在", code="NOT_FOUND")
        assert dto.success is False
        assert dto.code == "NOT_FOUND"
        assert dto.message == "人格不存在"
        assert dto.data is None

    async def test_error_defaults(self) -> None:
        """error 缺省：INTERNAL_ERROR / 系统异常。"""
        dto = ResultDTO.error()
        assert dto.code == "INTERNAL_ERROR"
        assert dto.message == "系统异常"

    async def test_json_contract_ok(self) -> None:
        """成功响应 JSON 契约（字段顺序无关，键集 + 值锁定）。"""
        payload = json.loads(ResultDTO.ok(data={"name": "小鱼"}).model_dump_json())
        assert payload == {
            "success": True,
            "code": "200",
            "message": "成功",
            "data": {"name": "小鱼"},
        }

    async def test_json_contract_error(self) -> None:
        """失败响应 JSON 契约。"""
        payload = json.loads(ResultDTO.error(message="冲突", code="CONFLICT").model_dump_json())
        assert payload == {
            "success": False,
            "code": "CONFLICT",
            "message": "冲突",
            "data": None,
        }

    async def test_generic_data_passthrough(self) -> None:
        """泛型 data 载荷透传（嵌套 dict / list）。"""
        dto = ResultDTO[list[dict[str, Any]]].ok(data=[{"a": 1}, {"b": 2}])
        assert dto.data == [{"a": 1}, {"b": 2}]


class TestPageResponse:
    async def test_of_offset_mode(self) -> None:
        """offset 模式：items/total/page/page_size 填充，无游标。"""
        page = PageResponse.of(items=[1, 2, 3], total=9, page=2, page_size=3)
        assert page.items == [1, 2, 3]
        assert page.total == 9
        assert page.page == 2
        assert page.page_size == 3
        assert page.next_cursor is None
        assert page.has_more is False

    async def test_of_defaults(self) -> None:
        """of 缺省 page/page_size。"""
        page = PageResponse.of(items=[], total=0)
        assert page.page == 1
        assert page.page_size == 20

    async def test_of_cursor_mode_with_more(self) -> None:
        """cursor 模式：next_cursor 非空 → has_more=True / page 恒 1。"""
        page = PageResponse.of_cursor(items=[1, 2], page_size=2, next_cursor="abc")
        assert page.has_more is True
        assert page.page == 1
        assert page.total == -1

    async def test_of_cursor_mode_end(self) -> None:
        """cursor 模式末页：next_cursor=None → has_more=False。"""
        page = PageResponse.of_cursor(items=[9], page_size=2, next_cursor=None, total=5)
        assert page.has_more is False
        assert page.total == 5

    async def test_field_defaults(self) -> None:
        """字段默认值：空 items / total=0 / page=1 / page_size=20。"""
        page = PageResponse()
        assert page.items == []
        assert page.total == 0
        assert page.page == 1
        assert page.page_size == 20
        assert page.next_cursor is None
        assert page.has_more is False

    async def test_negative_total_rejected_below_minus_one(self) -> None:
        """total 下界校验：ge=-1（-1=未计算哨兵，更小值非法）。"""
        with pytest.raises(ValueError):
            PageResponse(items=[], total=-2)

    async def test_zero_page_size_rejected(self) -> None:
        """page_size 下界校验：ge=1。"""
        with pytest.raises(ValueError):
            PageResponse(items=[], page_size=0)

    async def test_json_contract_cursor_mode(self) -> None:
        """cursor 模式 JSON 契约（完整示例）。"""
        page = PageResponse.of_cursor(items=["a", "b"], page_size=2, next_cursor="NA==")
        payload = json.loads(page.model_dump_json())
        assert payload == {
            "items": ["a", "b"],
            "total": -1,
            "page": 1,
            "page_size": 2,
            "next_cursor": "NA==",
            "has_more": True,
        }


class TestCursorCodec:
    async def test_roundtrip(self) -> None:
        """encode → decode 往返等价（键序无关）。"""
        payload = {"id": "abc", "ts": 1690000000000}
        decoded = decode_cursor(encode_cursor(payload))
        assert decoded == payload

    async def test_empty_payload_encodes_empty_string(self) -> None:
        """空 dict 编码为空字符串（首页无游标形态）。"""
        assert encode_cursor({}) == ""

    async def test_none_or_empty_decodes_none(self) -> None:
        """None / 空字符串解码为 None。"""
        assert decode_cursor(None) is None
        assert decode_cursor("") is None

    async def test_malformed_cursor_decodes_none(self) -> None:
        """非法 base64 / 非 JSON 输入容错为 None（不抛异常）。"""
        assert decode_cursor("!!!not-base64!!!") is None

    async def test_non_dict_json_decodes_none(self) -> None:
        """JSON 数组形态（非 dict）容错为 None。"""
        import base64

        raw = json.dumps([1, 2, 3]).encode("utf-8")
        cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        assert decode_cursor(cursor) is None

    async def test_urlsafe_alphabet(self) -> None:
        """编码输出 URL-safe 字母表（无 + / = 字符）。"""
        payload = {"id": "<<key?with/reserved>>"}
        encoded = encode_cursor(payload)
        assert not any(ch in encoded for ch in "+/=")
        assert decode_cursor(encoded) == payload
