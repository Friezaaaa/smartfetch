"""Native MCP Streamable HTTP transport for the SmartFetch retrieval engine."""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import wraps
import json
import time
from typing import Annotated, Any, Awaitable, Callable, Optional

from mcp.types import CallToolResult, TextContent
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field
from starlette.routing import Route
from x402.schemas import PaymentRequirements, ResourceInfo

from .bazaar import (
    FETCH_DESCRIPTION,
    FETCH_INPUT_EXAMPLE,
    FETCH_INPUT_SCHEMA,
    FETCH_OUTPUT_EXAMPLE,
    FETCH_OUTPUT_SCHEMA,
    MARKDOWN_DESCRIPTION,
    MARKDOWN_OUTPUT_EXAMPLE,
    MARKDOWN_OUTPUT_SCHEMA,
    RENDER_DESCRIPTION,
    RENDER_INPUT_EXAMPLE,
    RENDER_INPUT_SCHEMA,
    RENDER_OUTPUT_EXAMPLE,
    TEXT_DESCRIPTION,
    TEXT_OUTPUT_EXAMPLE,
    TEXT_OUTPUT_SCHEMA,
    mcp_discovery_extension,
)
from .activity import current_request_id, emit_activity, emit_v111_activity
from .config import (
    HOST,
    MAX_REQUEST_BODY_BYTES,
)
from .mcp_logging import configure_mcp_sdk_logging
from .payments import X402Settings, create_x402_resource_server
from .v111_contracts import (
    DirectExtractionRequest,
    SearchAndExtractRequest,
    V111_VARIANTS,
)
from .v111_service import V111ExecutionError, V111Service, run_v111_deadline


MCP_PATH = "/mcp"
MCP_TOOL = "fetch_webpage"
MCP_TOOLS = (
    MCP_TOOL,
    "webpage_to_markdown",
    "extract_webpage_text",
    "render_webpage",
)
V111_MCP_TOOLS = (
    "search_and_extract",
    "extract_structured_data",
)
MCP_TRANSPORT = "streamable-http"
MCP_DEFAULT_MAX_CHARS = 20000
MCP_MIN_CHARS = 1000
MCP_MAX_CHARS = 50000
FetchHandler = Callable[[str, bool, int], Awaitable[dict]]
COMMON_PROJECTION_FIELDS = (
    "success",
    "requested_url",
    "final_url",
    "status_code",
    "render_method",
    "elapsed_ms",
    "fallback_reason",
    "title",
    "truncated",
    "max_chars",
    "request_id",
    "service_version",
)


@dataclass
class SmartFetchMCP:
    fastmcp: FastMCP
    route: Route
    resource_server: Optional[object]
    accepts: list[PaymentRequirements]
    v111_accepts: dict[str, list[PaymentRequirements]] = field(
        default_factory=dict
    )

    @asynccontextmanager
    async def lifespan(self, _application):
        async with self.fastmcp.session_manager.run():
            yield


def _initialize_payment(settings: X402Settings):
    initialization_failed = False
    resource_server = None
    accepts = []
    try:
        from x402.server import ResourceConfig

        resource_server = create_x402_resource_server(
            settings,
            register_bazaar=True,
        )
        resource_server.initialize()
        accepts = resource_server.build_payment_requirements(ResourceConfig(
            scheme="exact",
            payTo=settings.pay_to,
            price=settings.price,
            network=settings.network,
        ))
    except Exception:
        initialization_failed = True

    if initialization_failed:
        raise RuntimeError("x402 MCP initialization failed")
    return resource_server, accepts


def _project_result(result: dict, primary_field: str) -> dict:
    fields = (*COMMON_PROJECTION_FIELDS, primary_field)
    return {key: result[key] for key in fields if key in result}


def _observe_execution(name: str, handler: FetchHandler):
    @wraps(handler)
    async def observed(**kwargs):
        started = time.perf_counter()
        emit_activity(
            "tool_started",
            transport="mcp",
            tool=name,
            stage="execution",
            outcome="started",
        )
        try:
            result = await handler(**kwargs)
        except Exception:
            emit_activity(
                "tool_failed",
                transport="mcp",
                tool=name,
                stage="execution",
                outcome="failed",
                failure_reason="retrieval_failed",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        emit_activity(
            "tool_completed",
            transport="mcp",
            tool=name,
            stage="execution",
            outcome="completed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    return observed


def _payment_result_details(result):
    challenge = getattr(result, "structuredContent", None)
    if challenge is None:
        challenge = getattr(result, "structured_content", None)
    if not isinstance(challenge, dict) or not challenge.get("accepts"):
        return None
    error = challenge.get("error")
    if isinstance(error, str):
        if error.startswith("Payment settlement failed"):
            return "settlement_failed", True
        if error.startswith("Payment verification failed"):
            return "verification_failed", True
        if error.startswith("Invalid payment payload"):
            return "invalid_payment", True
        if error.lower() != "payment required":
            return "payment_rejected", True
    return "payment_required", False


def _observe_payment_result(
    name: str,
    handler,
    settings: X402Settings,
    *,
    price: str | None = None,
    v111_definition=None,
):
    @wraps(handler)
    async def observed(**kwargs):
        result = await handler(**kwargs)
        details = _payment_result_details(result)
        if details is None:
            return result
        failure_reason, payment_present = details
        payment_fields = {
            "payment_present": payment_present,
            "payment_network": settings.network,
            "payment_asset": "USDC",
            "payment_amount": price or settings.price,
            "failure_reason": failure_reason,
        }
        emitter = emit_v111_activity if v111_definition is not None else emit_activity
        v111_fields = (
            {
                "capability": v111_definition.capability,
                "variant": v111_definition.variant,
            }
            if v111_definition is not None else {}
        )
        if failure_reason == "settlement_failed":
            emitter(
                "payment_settled",
                transport="mcp",
                tool=name,
                stage="settlement",
                outcome="failed",
                payment_stage="settlement",
                **payment_fields,
                **v111_fields,
            )
        else:
            emitter(
                "payment_challenged",
                transport="mcp",
                tool=name,
                stage="challenge",
                outcome="payment_required",
                payment_stage="challenge",
                **payment_fields,
                **v111_fields,
            )
        return result

    return observed


def create_smartfetch_mcp(
    settings: X402Settings,
    fetch_handler: FetchHandler,
    *,
    v111_service: V111Service | None = None,
) -> SmartFetchMCP:
    """Create the native MCP server and eagerly secure each paid tool."""
    mcp = FastMCP(
        "SmartFetch",
        host=HOST,
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
    )
    configure_mcp_sdk_logging()
    resource_server = None
    accepts = []
    v111_accepts: dict[str, list[PaymentRequirements]] = {}

    async def fetch_webpage(
        url: str,
        max_chars: Annotated[
            int,
            Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS),
        ] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> dict:
        return await fetch_handler(url, force_browser, max_chars)

    async def webpage_to_markdown(
        url: str,
        max_chars: Annotated[
            int,
            Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS),
        ] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> dict:
        result = await fetch_handler(url, force_browser, max_chars)
        return _project_result(result, "markdown")

    async def extract_webpage_text(
        url: str,
        max_chars: Annotated[
            int,
            Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS),
        ] = MCP_DEFAULT_MAX_CHARS,
        force_browser: bool = False,
    ) -> dict:
        result = await fetch_handler(url, force_browser, max_chars)
        return _project_result(result, "content")

    async def render_webpage(
        url: str,
        max_chars: Annotated[
            int,
            Field(ge=MCP_MIN_CHARS, le=MCP_MAX_CHARS),
        ] = MCP_DEFAULT_MAX_CHARS,
    ) -> dict:
        return await fetch_handler(url, True, max_chars)

    if settings.enabled:
        resource_server, accepts = _initialize_payment(settings)
        from x402.mcp import create_payment_wrapper

    def protect(name, description, handler, extension):
        observed_handler = _observe_execution(name, handler)
        if not settings.enabled:
            return observed_handler
        from x402.mcp import PaymentWrapperHooks

        def payment_verified(_context):
            emit_activity(
                "payment_verified",
                transport="mcp",
                tool=name,
                stage="verification",
                outcome="verified",
                payment_present=True,
                payment_stage="verification",
                payment_network=settings.network,
                payment_asset="USDC",
                payment_amount=settings.price,
            )
            return True

        def payment_settled(_context):
            emit_activity(
                "payment_settled",
                transport="mcp",
                tool=name,
                stage="settlement",
                outcome="settled",
                payment_present=True,
                payment_stage="settlement",
                payment_network=settings.network,
                payment_asset="USDC",
                payment_amount=settings.price,
            )

        payment_wrapper = create_payment_wrapper(
            resource_server,
            accepts=accepts,
            resource=ResourceInfo(
                url=f"mcp://tool/{name}",
                description=description,
                mimeType="application/json",
                serviceName="SmartFetch",
            ),
            hooks=PaymentWrapperHooks(
                on_before_execution=payment_verified,
                on_after_settlement=payment_settled,
            ),
            extensions=extension,
        )
        protected_handler = payment_wrapper(observed_handler)
        return _observe_payment_result(name, protected_handler, settings)

    tools = (
        (
            MCP_TOOL,
            FETCH_DESCRIPTION,
            fetch_webpage,
            FETCH_INPUT_SCHEMA,
            FETCH_INPUT_EXAMPLE,
            FETCH_OUTPUT_SCHEMA,
            FETCH_OUTPUT_EXAMPLE,
        ),
        (
            "webpage_to_markdown",
            MARKDOWN_DESCRIPTION,
            webpage_to_markdown,
            FETCH_INPUT_SCHEMA,
            FETCH_INPUT_EXAMPLE,
            MARKDOWN_OUTPUT_SCHEMA,
            MARKDOWN_OUTPUT_EXAMPLE,
        ),
        (
            "extract_webpage_text",
            TEXT_DESCRIPTION,
            extract_webpage_text,
            FETCH_INPUT_SCHEMA,
            FETCH_INPUT_EXAMPLE,
            TEXT_OUTPUT_SCHEMA,
            TEXT_OUTPUT_EXAMPLE,
        ),
        (
            "render_webpage",
            RENDER_DESCRIPTION,
            render_webpage,
            RENDER_INPUT_SCHEMA,
            RENDER_INPUT_EXAMPLE,
            FETCH_OUTPUT_SCHEMA,
            RENDER_OUTPUT_EXAMPLE,
        ),
    )
    for (
        name,
        description,
        handler,
        input_schema,
        input_example,
        output_schema,
        output_example,
    ) in tools:
        extension = mcp_discovery_extension(
            tool_name=name,
            description=description,
            input_schema=input_schema,
            input_example=input_example,
            output_schema=output_schema,
            output_example=output_example,
            transport=MCP_TRANSPORT,
        )
        tool_handler = protect(name, description, handler, extension)
        mcp.tool(
            name=name,
            description=description,
            structured_output=False,
        )(tool_handler)

    if settings.enabled and type(v111_service) is V111Service:
        from x402.mcp import PaymentWrapperHooks
        from x402.server import ResourceConfig

        def error_result(code: str) -> CallToolResult:
            normalized = V111ExecutionError(code)
            body = {
                "success": False,
                "error_code": normalized.code,
                "error": normalized.public_message,
            }
            return CallToolResult(
                isError=True,
                content=[TextContent(
                    type="text",
                    text=json.dumps(body, separators=(",", ":")),
                )],
                structuredContent=body,
            )

        def make_runner(definition):
            async def run_v111(*, request):
                started = time.perf_counter()
                emit_v111_activity(
                    "tool_started",
                    transport="mcp",
                    tool=definition.capability,
                    stage="execution",
                    outcome="started",
                    capability=definition.capability,
                    variant=definition.variant,
                )
                try:
                    async def execute_and_serialize():
                        def activity_sink(event, **fields):
                            emit_v111_activity(
                                event,
                                transport="mcp",
                                tool=definition.capability,
                                capability=definition.capability,
                                variant=definition.variant,
                                **fields,
                            )

                        if definition.capability == "search_and_extract":
                            result = await v111_service.execute_search(
                                request,
                                request_id=current_request_id() or "mcp-request",
                                activity_sink=activity_sink,
                            )
                        else:
                            result = await v111_service.execute_extraction(
                                request,
                                request_id=current_request_id() or "mcp-request",
                                activity_sink=activity_sink,
                            )
                        return result.model_dump(mode="json")

                    payload = await run_v111_deadline(
                        definition.variant,
                        execute_and_serialize(),
                    )
                except V111ExecutionError as exc:
                    emit_v111_activity(
                        "tool_failed",
                        transport="mcp",
                        tool=definition.capability,
                        stage="execution",
                        outcome="failed",
                        duration_ms=(time.perf_counter() - started) * 1000,
                        capability=definition.capability,
                        variant=definition.variant,
                    )
                    return error_result(exc.code)
                except Exception:
                    emit_v111_activity(
                        "tool_failed",
                        transport="mcp",
                        tool=definition.capability,
                        stage="execution",
                        outcome="failed",
                        duration_ms=(time.perf_counter() - started) * 1000,
                        capability=definition.capability,
                        variant=definition.variant,
                    )
                    return error_result("invalid_provider_output")
                emit_v111_activity(
                    "tool_completed",
                    transport="mcp",
                    tool=definition.capability,
                    stage="execution",
                    outcome="completed",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    capability=definition.capability,
                    variant=definition.variant,
                )
                return payload

            return run_v111

        def make_wrapper(definition):
            variant_accepts = resource_server.build_payment_requirements(
                ResourceConfig(
                    scheme="exact",
                    payTo=settings.pay_to,
                    price=definition.price,
                    network=settings.network,
                )
            )
            v111_accepts[definition.mcp_resource] = variant_accepts

            def verified(context):
                if not v111_service.claim_payment_attempt(context.payment_payload):
                    return False
                emit_v111_activity(
                    "payment_verified",
                    transport="mcp",
                    tool=definition.capability,
                    stage="verification",
                    outcome="verified",
                    payment_present=True,
                    payment_stage="verification",
                    payment_network=settings.network,
                    payment_asset="USDC",
                    payment_amount=definition.price,
                    capability=definition.capability,
                    variant=definition.variant,
                )
                return True

            def settled(_context):
                emit_v111_activity(
                    "payment_settled",
                    transport="mcp",
                    tool=definition.capability,
                    stage="settlement",
                    outcome="settled",
                    payment_present=True,
                    payment_stage="settlement",
                    payment_network=settings.network,
                    payment_asset="USDC",
                    payment_amount=definition.price,
                    capability=definition.capability,
                    variant=definition.variant,
                )

            wrapper = create_payment_wrapper(
                resource_server,
                accepts=variant_accepts,
                resource=ResourceInfo(
                    url=definition.mcp_resource,
                    description=(
                        f"SmartFetch {definition.capability} "
                        f"{definition.variant}"
                    ),
                    mimeType="application/json",
                    serviceName="SmartFetch",
                ),
                hooks=PaymentWrapperHooks(
                    on_before_execution=verified,
                    on_after_settlement=settled,
                ),
            )(make_runner(definition))
            return _observe_payment_result(
                definition.capability,
                wrapper,
                settings,
                price=definition.price,
                v111_definition=definition,
            )

        wrappers = {
            definition.variant: make_wrapper(definition)
            for definition in V111_VARIANTS
        }

        async def search_and_extract(
            query: str,
            mode: str,
            max_results: int = 5,
            max_sources: int | None = None,
            domains: list[str] | None = None,
            freshness: str | None = None,
            json_schema: dict[str, Any] | None = None,
            instructions: str | None = None,
            *,
            ctx: Context,
        ) -> dict:
            try:
                request = SearchAndExtractRequest(
                    query=query,
                    mode=mode,
                    max_results=max_results,
                    max_sources=max_sources,
                    domains=domains,
                    freshness=freshness,
                    json_schema=json_schema,
                    instructions=instructions,
                )
                v111_service.require_ready(request.mode)
            except V111ExecutionError as exc:
                return error_result(exc.code)
            except Exception:
                return error_result("invalid_request")
            return await wrappers[request.mode](request=request, ctx=ctx)

        async def extract_structured_data(
            source_type: str,
            source_url: str,
            json_schema: dict[str, Any],
            render_mode: str | None = None,
            instructions: str | None = None,
            *,
            ctx: Context,
        ) -> dict:
            try:
                request = DirectExtractionRequest(
                    source_type=source_type,
                    source_url=source_url,
                    json_schema=json_schema,
                    render_mode=render_mode,
                    instructions=instructions,
                )
                v111_service.require_ready(request.source_type)
            except V111ExecutionError as exc:
                return error_result(exc.code)
            except Exception:
                return error_result("invalid_request")
            return await wrappers[request.source_type](request=request, ctx=ctx)

        mcp.tool(
            name="search_and_extract",
            description=(
                "Search public sources and return results, an answer, "
                "or schema-valid data."
            ),
            structured_output=False,
        )(search_and_extract)
        mcp.tool(
            name="extract_structured_data",
            description=(
                "Extract schema-valid data from one public webpage or "
                "media source."
            ),
            structured_output=False,
        )(extract_structured_data)

    http_app = mcp.streamable_http_app()
    route = next(
        candidate
        for candidate in http_app.routes
        if isinstance(candidate, Route) and candidate.path == MCP_PATH
    )
    return SmartFetchMCP(
        mcp,
        route,
        resource_server,
        accepts,
        v111_accepts,
    )
