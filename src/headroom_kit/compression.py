"""Allow recoverable Responses compression for clients with a retrieval tool."""

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def protect_unretrievable_routes(app: "FastAPI") -> None:
    from headroom.transforms.compression_units import find_content_router

    proxy = app.state.proxy
    native = proxy.openai_pipeline
    router = find_content_router(native)
    proxy.openai_pipeline = proxy._derived_compress_pipeline("kit-openai-lossless", lossless=True)
    if router is None:
        return
    compress = proxy._compress_openai_responses_live_text_units_with_router.__func__
    # Responses has a separate 512-byte gate. Use the profile's block floor so
    # modest RTK/context-mode results can reach the router too.
    proxy.OPENAI_RESPONSES_ROUTER_MIN_BYTES = router.config.min_chars_for_block_compression

    def compress_responses(
        payload: dict[str, object],
        *,
        model: str,
        request_id: str,
        pass_id: str | None = None,
        timing: dict[str, float] | None = None,
    ) -> tuple[dict[str, object], bool, int, list[str], dict[str, int], list[str], int]:
        # A request-local view keeps concurrent clients from changing each
        # other's policy. Both HTTP and WebSocket Responses use this method.
        view = copy.copy(proxy)
        tool_groups = [payload.get("tools")]
        items = payload.get("input")
        if isinstance(items, list):
            # Inspect Codex's transcript carriers without changing wire shape.
            tool_groups.extend(
                item.get("tools")
                for item in items
                if isinstance(item, dict) and item.get("type") == "additional_tools"
            )
        if (
            router.config.ccr_enabled
            and router.config.ccr_inject_marker
            and any(has_retrieval_tool(tools) for tools in tool_groups)
        ):
            view.openai_pipeline = native
        return compress(
            view, payload, model=model, request_id=request_id, pass_id=pass_id, timing=timing
        )

    proxy._compress_openai_responses_live_text_units_with_router = compress_responses


def has_retrieval_tool(tools: object) -> bool:
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        definition = tool.get("function", tool)
        if not isinstance(definition, dict):
            continue
        name = definition.get("name", "")
        if isinstance(name, str) and (
            name == "headroom_retrieve"
            or (name.startswith("mcp__") and name.endswith("__headroom_retrieve"))
        ):
            return True
        if tool.get("type") == "namespace" and has_retrieval_tool(tool.get("tools")):
            return True
    return False
