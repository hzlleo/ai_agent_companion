"""harness/llm logprobs 传输与置信度派生测试（MockTransport 零真实出站）。

覆盖面：
- 非流式请求强制携带 ``logprobs`` / ``top_logprobs`` / ``prompt_logprobs``；
  流式请求恒省略 ``prompt_logprobs``（vLLM 400 约束）但仍携带
  ``logprobs`` / ``top_logprobs``；构造旋钮 None 省略对应字段；
- 响应 logprobs 解析（content 含 top 候选、prompt_logprobs 含 null / 空
  dict 占位且位次不左移）；
- 平均对数概率与置信度派生（exp 均值、正均值上溢短路、NaN 过滤）；
- 缺失 / 畸形容忍（真缺失跳过且不抛；空 token / 0.0 合法值保留）与
  流式块 logprobs 捕获。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from packages.harness.config.llm_wire import LlmClientConfigModel
from packages.harness.llm.client import LlmHttpClient
from packages.harness.llm.llm_models import (
    DEFAULT_PROMPT_LOGPROBS,
    DEFAULT_TOP_LOGPROBS,
    ChatMessageModel,
    ChatMessageRole,
    ChatRequestParams,
    ToolDefinitionModel,
    build_chat_request_body,
)

#: MockTransport handler 类型（请求 → 响应）。
Handler = Callable[[httpx.Request], httpx.Response]


def _wire() -> LlmClientConfigModel:
    """构造测试 wire 配置。"""
    return LlmClientConfigModel(
        provider="openai_compatible",
        base_url="http://llm.test/v1",
        api_key="sk-test",
        model="test-model",
    )


def _user_message() -> list[ChatMessageModel]:
    """单条用户消息。"""
    return [ChatMessageModel(role=ChatMessageRole.USER, content="你好")]


def _content_logprobs_response(
    content: list[dict[str, Any]],
    *,
    prompt_logprobs: list[Any] | None = None,
) -> httpx.Response:
    """构造只含 logprobs 面的非流式响应（畸形 / 边界用例驱动）。"""
    payload: dict[str, Any] = {
        "id": "resp-lp-edge",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "片段"},
                "finish_reason": "stop",
                "logprobs": {"content": content},
            }
        ],
    }
    if prompt_logprobs is not None:
        payload["prompt_logprobs"] = prompt_logprobs
    return httpx.Response(
        200,
        content=json.dumps(payload, ensure_ascii=False, allow_nan=True).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _logprob_response() -> httpx.Response:
    """构造带 content logprobs 与 prompt_logprobs 的非流式响应（真机形态裁剪）。"""
    return httpx.Response(
        200,
        json={
            "id": "resp-lp",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "量子计算"},
                    "finish_reason": "stop",
                    "logprobs": {
                        "content": [
                            {
                                "token": "量子",
                                "logprob": -0.001,
                                "bytes": [233, 135, 143, 229, 173, 144],
                                "top_logprobs": [
                                    {
                                        "token": "量子",
                                        "logprob": -0.001,
                                        "bytes": [233, 135, 143, 229, 173, 144],
                                    },
                                    {"token": "利用", "logprob": -6.9, "bytes": None},
                                ],
                            },
                            {"token": "计算", "logprob": -0.101},
                        ]
                    },
                }
            ],
            "usage": {"prompt_tokens": 23, "completion_tokens": 2, "total_tokens": 25},
            "prompt_logprobs": [
                None,
                {"128799": {"logprob": -20.89, "rank": 127879, "decoded_token": "的"}},
                {"5": {"logprob": -1.58, "rank": 1, "decoded_token": "#"}},
            ],
        },
    )


@pytest.fixture
async def make_client() -> Any:
    """client 工厂（每用例注入 MockTransport 共享 client；用后统一关闭）。"""
    created: list[httpx.AsyncClient] = []

    def _make(handler: Handler, **kwargs: Any) -> LlmHttpClient:
        shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created.append(shared)
        return LlmHttpClient(_wire(), shared, **kwargs)

    yield _make
    for shared in created:
        await shared.aclose()


# ---------------------------------------------------------------------------
# 请求体：强制字段
# ---------------------------------------------------------------------------


async def test_build_request_forces_logprobs_fields() -> None:
    """默认请求体：三字段全携带（强制面——端点仅显式声明才回传数据）。"""
    body = build_chat_request_body("m", _user_message(), None, stream=False)
    assert body["logprobs"] is True
    assert body["top_logprobs"] == DEFAULT_TOP_LOGPROBS == 20
    assert body["prompt_logprobs"] == DEFAULT_PROMPT_LOGPROBS == 20


async def test_build_request_stream_omits_prompt_logprobs() -> None:
    """流式请求恒省 prompt_logprobs（vLLM stream=true + >0 直接 400）。"""
    stream_body = build_chat_request_body("m", _user_message(), None, stream=True)
    assert stream_body["logprobs"] is True
    assert stream_body["top_logprobs"] == 20
    assert "prompt_logprobs" not in stream_body

    explicit_body = build_chat_request_body(
        "m", _user_message(), None, stream=True, prompt_logprobs=5
    )
    assert explicit_body["top_logprobs"] == 20
    assert "prompt_logprobs" not in explicit_body

    tool = ToolDefinitionModel(name="t", description="d")
    tool_body = build_chat_request_body(
        "m", _user_message(), None, stream=False, tools=[tool]
    )
    assert tool_body["logprobs"] is True
    assert tool_body["top_logprobs"] == 20
    assert tool_body["prompt_logprobs"] == 20


async def test_chat_client_both_knobs_none_omits_all_logprobs(
    make_client: Any,
) -> None:
    """client 双旋钮 None：非流式请求整体不声明 logprobs 面。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp-plain",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "好"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    client = make_client(handler, top_logprobs=None, prompt_logprobs=None)
    await client.chat(_user_message())
    sent = json.loads(captured[0].content)
    assert "logprobs" not in sent
    assert "top_logprobs" not in sent
    assert "prompt_logprobs" not in sent


async def test_stream_client_never_sends_prompt_logprobs(make_client: Any) -> None:
    """client.stream 默认旋钮：流式体含 logprobs/top，绝不含 prompt_logprobs。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        lines = [
            f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': '好'}}]})}",
            "data: [DONE]",
            "",
        ]
        return httpx.Response(
            200,
            text="\n".join(lines),
            headers={"content-type": "text/event-stream"},
        )

    client = make_client(handler)
    chunks = [chunk async for chunk in client.stream(_user_message())]
    assert chunks[0].content == "好"
    sent = json.loads(captured[0].content)
    assert sent["stream"] is True
    assert sent["logprobs"] is True
    assert sent["top_logprobs"] == 20
    assert "prompt_logprobs" not in sent


async def test_build_request_knob_none_omits_individual_fields() -> None:
    """旋钮 None：仅省略对应字段（logprobs 仍在以声明另一项）。"""
    body = build_chat_request_body(
        "m",
        _user_message(),
        None,
        stream=False,
        top_logprobs=None,
        prompt_logprobs=5,
    )
    assert "top_logprobs" not in body
    assert body["prompt_logprobs"] == 5
    assert body["logprobs"] is True


async def test_build_request_logprobs_disabled_omits_all() -> None:
    """logprobs=False：三字段全省略（旧端点兼容逃生口）。"""
    body = build_chat_request_body(
        "m", _user_message(), None, stream=False, logprobs=False
    )
    assert "logprobs" not in body
    assert "top_logprobs" not in body
    assert "prompt_logprobs" not in body


# ---------------------------------------------------------------------------
# 响应解析与置信度派生
# ---------------------------------------------------------------------------


async def test_chat_parses_logprobs_and_derives_confidence(
    make_client: Any,
) -> None:
    """响应 logprobs 解析：content 含候选、prompt 含 null 占位、置信度精确派生。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _logprob_response()

    client = make_client(handler)
    response = await client.chat(
        _user_message(), ChatRequestParams(temperature=0.7, max_tokens=64)
    )

    sent = json.loads(captured[0].content)
    assert sent["logprobs"] is True
    assert sent["top_logprobs"] == 20
    assert sent["prompt_logprobs"] == 20

    assert response.content_logprobs is not None
    assert [entry.token for entry in response.content_logprobs] == ["量子", "计算"]
    assert response.content_logprobs[0].top_logprobs[1].token == "利用"
    assert response.prompt_logprobs is not None
    assert response.prompt_logprobs[0] is None
    assert response.prompt_logprobs[1] is not None
    assert response.prompt_logprobs[1].token_id == 128799
    assert response.prompt_logprobs[1].rank == 127879

    expected_mean = (-0.001 + -0.101) / 2
    assert response.mean_token_logprob == pytest.approx(expected_mean)
    assert response.confidence == pytest.approx(math.exp(expected_mean))


async def test_chat_tolerates_absent_logprobs(make_client: Any) -> None:
    """旧端点不回调：字段为 None、置信度 None、回复本体不受影响。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-plain",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "你好呀"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    client = make_client(handler)
    response = await client.chat(_user_message())
    assert response.content == "你好呀"
    assert response.content_logprobs is None
    assert response.prompt_logprobs is None
    assert response.mean_token_logprob is None
    assert response.confidence is None


async def test_confidence_positive_mean_overflow_short_circuits(
    make_client: Any,
) -> None:
    """平均对数概率 > 0（如 1e308）不调用 exp：不抛异常且置信度 1.0。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _content_logprobs_response([{"token": "极", "logprob": 1e308}])

    client = make_client(handler)
    response = await client.chat(_user_message())
    assert response.mean_token_logprob == 1e308
    assert response.confidence == 1.0


async def test_confidence_filters_nan_and_keeps_negative_infinity(
    make_client: Any,
) -> None:
    """派生过滤 NaN（丢弃）但保留 -inf（exp 下溢为 0.0——语义正确）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _content_logprobs_response(
            [
                {"token": "A", "logprob": float("nan")},
                {"token": "B", "logprob": -2.0},
                {"token": "C", "logprob": -math.inf},
            ]
        )

    client = make_client(handler)
    response = await client.chat(_user_message())
    assert response.content_logprobs is not None
    assert len(response.content_logprobs) == 3
    assert response.mean_token_logprob == -math.inf
    assert response.confidence == 0.0


async def test_confidence_all_nan_yields_none(make_client: Any) -> None:
    """全组 NaN：均值与置信度为 None（不把 NaN 泄漏给消费方）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _content_logprobs_response(
            [
                {"token": "A", "logprob": float("nan")},
                {"token": "B", "logprob": float("nan")},
            ]
        )

    client = make_client(handler)
    response = await client.chat(_user_message())
    assert response.mean_token_logprob is None
    assert response.confidence is None


async def test_content_logprobs_skips_true_missing_only(
    make_client: Any,
) -> None:
    """真缺失（logprob 为 null / 键缺失）跳过；空 token 与 0.0 保留。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _content_logprobs_response(
            [
                {"token": "", "logprob": 0.0},
                {"token": "空值", "logprob": None},
                {"token": "无键"},
                {"token": "正常", "logprob": -1.0},
            ]
        )

    client = make_client(handler)
    response = await client.chat(_user_message())
    assert response.content_logprobs is not None
    assert [entry.token for entry in response.content_logprobs] == ["", "正常"]
    assert [entry.logprob for entry in response.content_logprobs] == [0.0, -1.0]
    assert response.mean_token_logprob == pytest.approx(-0.5)


async def test_prompt_logprobs_empty_dicts_keep_positions(make_client: Any) -> None:
    """空 dict / 无 int 键元素占位 None——有效条目仍落在原提示词位次。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _content_logprobs_response(
            [{"token": "答", "logprob": -0.1}],
            prompt_logprobs=[
                None,
                {},
                {"bad-key": {"logprob": -9.0, "rank": 1, "decoded_token": "坏"}},
                {"5": {"logprob": -1.58, "rank": 1, "decoded_token": "#"}},
                None,
            ],
        )

    client = make_client(handler)
    response = await client.chat(_user_message())
    assert response.prompt_logprobs is not None
    assert len(response.prompt_logprobs) == 5
    assert [entry is None for entry in response.prompt_logprobs] == [
        True,
        True,
        True,
        False,
        True,
    ]
    valid = response.prompt_logprobs[3]
    assert valid is not None
    assert valid.token_id == 5
    assert valid.logprob == pytest.approx(-1.58)


async def test_prompt_logprobs_length_mismatch_does_not_raise(
    make_client: Any,
) -> None:
    """prompt_logprobs 与提示词长度不一致：按回传位次归一，不抛异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _content_logprobs_response(
            [{"token": "答", "logprob": -0.1}],
            prompt_logprobs=[
                None,
                {"7": {"logprob": -1.5, "rank": 2, "decoded_token": "你"}},
            ],
        )

    client = make_client(handler)
    response = await client.chat(_user_message())
    assert response.prompt_logprobs is not None
    assert len(response.prompt_logprobs) == 2
    assert response.prompt_logprobs[0] is None
    assert response.prompt_logprobs[1] is not None
    assert response.prompt_logprobs[1].token_id == 7


async def test_stream_chunk_carries_logprobs_best_effort(make_client: Any) -> None:
    """流式块：增量 logprobs 与首块 prompt_logprobs 尽力捕获（不破坏传输）。"""

    first_chunk = {
        "choices": [
            {
                "index": 0,
                "delta": {"content": "量"},
                "finish_reason": None,
                "logprobs": {"content": [{"token": "量", "logprob": -0.2}]},
            }
        ],
        "prompt_logprobs": [
            None,
            {"7": {"logprob": -1.5, "rank": 2, "decoded_token": "你"}},
        ],
    }
    second_chunk = {
        "choices": [
            {"index": 0, "delta": {"content": "子"}, "finish_reason": "stop"}
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        lines = [
            f"data: {json.dumps(first_chunk, ensure_ascii=False)}",
            f"data: {json.dumps(second_chunk, ensure_ascii=False)}",
            "data: [DONE]",
            "",
        ]
        return httpx.Response(
            200,
            text="\n".join(lines),
            headers={"content-type": "text/event-stream"},
        )

    client = make_client(handler)
    chunks = [chunk async for chunk in client.stream(_user_message())]
    first = chunks[0]
    assert first.content == "量"
    assert first.content_logprobs is not None
    assert first.content_logprobs[0].token == "量"
    assert first.prompt_logprobs is not None
    assert first.prompt_logprobs[0] is None
    assert first.prompt_logprobs[1] is not None
    assert first.prompt_logprobs[1].token_id == 7
    assert chunks[1].content_logprobs is None
