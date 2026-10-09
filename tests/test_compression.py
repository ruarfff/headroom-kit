"""Responses compression must be recoverable and isolated per request."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig

from headroom_kit.proxy import protect_unretrievable_routes


@dataclass
class ProxyFixture:
    native: list[ContentRouter]
    lossless: list[ContentRouter] = field(default_factory=list)
    calls: list[tuple[str, str, str | None, dict[str, float] | None]] = field(default_factory=list)
    OPENAI_RESPONSES_ROUTER_MIN_BYTES: int = 512
    openai_pipeline: list[ContentRouter] = field(init=False)

    def __post_init__(self) -> None:
        self.openai_pipeline = self.native

    def _derived_compress_pipeline(self, name: str, *, lossless: bool) -> list[ContentRouter]:
        assert name == "kit-openai-lossless" and lossless
        return self.lossless

    def _compress_openai_responses_live_text_units_with_router(
        self,
        payload: dict[str, object],
        *,
        model: str,
        request_id: str,
        pass_id: str | None = None,
        timing: dict[str, float] | None = None,
    ) -> tuple[dict[str, object], bool, int, list[str], dict[str, int], list[str], int]:
        self.calls.append((model, request_id, pass_id, timing))
        return payload, self.openai_pipeline is self.native, 0, [], {}, [], 0


def proxy_fixture(
    *, router_enabled: bool = True, ccr_enabled: bool = True, inject_marker: bool = True
) -> ProxyFixture:
    native = (
        [
            ContentRouter(
                ContentRouterConfig(
                    ccr_enabled=ccr_enabled,
                    ccr_inject_marker=inject_marker,
                    min_chars_for_block_compression=128,
                )
            )
        ]
        if router_enabled
        else []
    )
    proxy = ProxyFixture(native)
    app = FastAPI()
    app.state.proxy = proxy
    protect_unretrievable_routes(app)
    return proxy


def compress(proxy: ProxyFixture, payload: dict[str, object]) -> bool:
    return proxy._compress_openai_responses_live_text_units_with_router(
        payload, model="test-model", request_id="test-request"
    )[1]


@pytest.mark.parametrize(
    "tools",
    [
        [{"type": "function", "name": "headroom_retrieve"}],
        [{"type": "function", "name": "mcp__headroom_kit__headroom_retrieve"}],
        [{"type": "function", "function": {"name": "headroom_retrieve"}}],
        [
            {
                "type": "namespace",
                "name": "headroom",
                "tools": [{"type": "function", "name": "headroom_retrieve"}],
            }
        ],
    ],
)
def test_advertised_retrieval_tools_enable_native_responses_compression(tools: object) -> None:
    proxy = proxy_fixture()
    assert compress(proxy, {"tools": tools})
    assert proxy.openai_pipeline is proxy.lossless


def test_transcript_tool_carriers_enable_native_responses_compression() -> None:
    proxy = proxy_fixture()
    payload = {
        "input": [
            {
                "type": "additional_tools",
                "tools": [{"type": "function", "name": "mcp__headroom_kit__headroom_retrieve"}],
            }
        ]
    }
    assert compress(proxy, payload)
    assert proxy.openai_pipeline is proxy.lossless


@pytest.mark.parametrize(
    "tools",
    [
        None,
        {},
        "headroom_retrieve",
        [None, 1, "headroom_retrieve"],
        [{"name": "retrieve_something_else"}],
        [{"name": 42}],
        [{"function": None}],
        [{"name": "not_mcp__headroom_retrieve"}],
        [{"type": "namespace", "tools": {"name": "headroom_retrieve"}}],
    ],
)
def test_requests_without_a_valid_retrieval_tool_stay_lossless(tools: object) -> None:
    proxy = proxy_fixture()
    assert not compress(proxy, {"tools": tools})
    assert proxy.openai_pipeline is proxy.lossless


def test_unrelated_input_items_do_not_advertise_tools() -> None:
    proxy = proxy_fixture()
    assert not compress(
        proxy,
        {
            "input": [
                None,
                {"type": "message", "tools": [{"name": "headroom_retrieve"}]},
                {"type": "additional_tools", "tools": None},
            ]
        },
    )


@pytest.mark.parametrize("ccr_enabled,inject_marker", [(False, True), (True, False)])
def test_retrieval_requires_ccr_and_marker_injection(
    ccr_enabled: bool, inject_marker: bool
) -> None:
    proxy = proxy_fixture(ccr_enabled=ccr_enabled, inject_marker=inject_marker)
    assert not compress(proxy, {"tools": [{"name": "headroom_retrieve"}]})


def test_missing_router_stays_lossless() -> None:
    proxy = proxy_fixture(router_enabled=False)
    assert not compress(proxy, {"tools": [{"name": "headroom_retrieve"}]})
    assert proxy.OPENAI_RESPONSES_ROUTER_MIN_BYTES == 512


def test_responses_size_gate_uses_the_selected_profile_floor() -> None:
    proxy = proxy_fixture()
    assert proxy.OPENAI_RESPONSES_ROUTER_MIN_BYTES == 128


def test_wrapper_preserves_request_arguments_and_result() -> None:
    proxy = proxy_fixture()
    payload: dict[str, object] = {"tools": [{"name": "headroom_retrieve"}]}
    timing = {"before": 0.5}
    result = proxy._compress_openai_responses_live_text_units_with_router(
        payload, model="test-model", request_id="request", pass_id="pass", timing=timing
    )
    assert result == (payload, True, 0, [], {}, [], 0)
    assert result[0] is payload
    assert proxy.calls == [("test-model", "request", "pass", timing)]
    assert proxy.calls[0][3] is timing


def test_concurrent_requests_do_not_change_the_shared_chat_pipeline() -> None:
    proxy = proxy_fixture()
    payloads: list[dict[str, object]] = [
        {"tools": [{"name": "headroom_retrieve"}]} if index % 2 else {} for index in range(40)
    ]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda payload: compress(proxy, payload), payloads))
    assert results == [bool(index % 2) for index in range(40)]
    assert proxy.openai_pipeline is proxy.lossless
