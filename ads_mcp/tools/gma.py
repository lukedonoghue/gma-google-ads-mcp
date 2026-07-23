"""Small customer-facing GMA router for deterministic specialist runs."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp.changesets import ChangesetError
from ads_mcp.gma_runtime import (
    AccountGateway,
    GmaRuntimeError,
    ScopeGateway,
    attach_run_to_workspace as attach_gma_run_to_workspace,
    build_goal_report as build_gma_goal_report,
    create_workspace as create_gma_workspace,
    get_run as get_gma_run,
    get_workspace as get_gma_workspace,
    list_goal_benchmarks as list_gma_goal_benchmarks,
    preflight as build_preflight,
    render_run_result,
    render_run as render_gma_run,
    run_skill as run_gma_skill,
    save_prepared_scope,
    update_workspace_actions as update_gma_workspace_actions,
)
from ads_mcp.skill_runs.budget_reallocator import BudgetAnalysisError

gma_mcp = FastMCP("gma", mask_error_details=True)


def _with_authoritative_report(result: dict[str, Any]) -> dict[str, Any]:
    """Return a compact report envelope while retaining the full run server-side."""

    return {
        "contract_version": result["contract_version"],
        "run_id": result["run_id"],
        "runtime_version": result["runtime_version"],
        "methodology_version": result["methodology_version"],
        "module": result["module"],
        "status": result["status"],
        "scope": result["scope"],
        "data_receipt": result["data_receipt"],
        "coverage": result["coverage"],
        "assessment": result["assessment"],
        "recommendations": result["recommendations"],
        "recovery_actions": result["recovery_actions"],
        "change_plan": result["change_plan"],
        "core_signature": result["core_signature"],
        "full_result_tool": {
            "name": "gma_get_run",
            "run_id": result["run_id"],
            "purpose": (
                "Retrieve the complete validated check ledger for the GMA "
                "workspace or a detailed evidence review."
            ),
        },
        "authoritative_report": {
            "content_type": "text/markdown",
            "presentation_rule": (
                "Show this report as returned. Do not omit, summarize, or "
                "rewrite its Recovery plan."
            ),
            "content": render_run_result(result),
        },
    }


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def preflight(host_package_version: str | None = None) -> dict[str, Any]:
    """Check GMA connector identity, versions, account boundary, and safe capabilities."""

    return build_preflight(host_package_version)


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def list_accounts() -> dict[str, Any]:
    """List non-manager Google Ads advertisers inside the GMA account boundary."""

    try:
        return AccountGateway().list_advertisers()
    except (GmaRuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def prepare_scope(
    customer_id: str,
    login_customer_id: str | None = None,
    analysis_start: str | None = None,
    analysis_end: str | None = None,
    campaign_ids: list[str] | None = None,
    business_mode: str | None = None,
) -> dict[str, Any]:
    """Return a confirmable advertiser scope plus type/status/trailing-30d spend."""

    try:
        prepared = ScopeGateway().prepare(
            customer_id=customer_id,
            login_customer_id=login_customer_id,
            analysis_start=analysis_start,
            analysis_end=analysis_end,
            campaign_ids=campaign_ids,
            business_mode=business_mode,
        )
        return await save_prepared_scope(prepared)
    except (GmaRuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def list_goal_benchmarks(business_mode: str) -> dict[str, Any]:
    """List source-labelled industry categories for a CPA or ROAS comparison."""

    try:
        return list_gma_goal_benchmarks(business_mode)
    except (GmaRuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def build_goal_report(
    scope_id: str,
    confirmed_scope_hash: str,
    industry_profile_id: str,
    confirmed_target_cpa: float | None = None,
    confirmed_target_roas: float | None = None,
    economics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare a confirmed goal with live results and sourced industry context.

    This report is read-only. The user-confirmed target stays authoritative;
    industry averages are advisory and are never written to Google Ads.
    """

    try:
        return await build_gma_goal_report(
            scope_id=scope_id,
            confirmed_scope_hash=confirmed_scope_hash,
            industry_profile_id=industry_profile_id,
            confirmed_target_cpa=confirmed_target_cpa,
            confirmed_target_roas=confirmed_target_roas,
            economics=economics,
        )
    except (GmaRuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def run_skill(
    module_id: str,
    scope_id: str,
    confirmed_scope_hash: str,
    business_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one registered GMA module and return a compact finished report.

    This creates a review-only Change Plan but never changes Google Ads. The
    hosted runtime owns queries, normalization, calculations, gates, IDs, and
    applyability. The `authoritative_report` field is the report Claude or Codex
    must show rather than rewriting the raw result. It always includes the
    recovery plan when a safety gate blocks a recommendation. Use `gma_get_run`
    only when the complete validated evidence ledger is needed.
    """

    try:
        result = await run_gma_skill(
            module_id=module_id,
            scope_id=scope_id,
            confirmed_scope_hash=confirmed_scope_hash,
            business_inputs=business_inputs,
        )
        return _with_authoritative_report(result)
    except (
        GmaRuntimeError,
        BudgetAnalysisError,
        ChangesetError,
        ValueError,
    ) as error:
        raise ToolError(str(error)) from error


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def get_run(run_id: str) -> dict[str, Any]:
    """Return a persisted, owner-bound, schema-validated GMA run result."""

    try:
        return await get_gma_run(run_id)
    except (GmaRuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def render_run(run_id: str) -> dict[str, Any]:
    """Render a canonical GMA run as a nontechnical Markdown report."""

    try:
        return await render_gma_run(run_id)
    except (GmaRuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def create_workspace(
    scope_id: str,
    confirmed_scope_hash: str,
) -> dict[str, Any]:
    """Create or reopen the persistent GMA dashboard workspace for a scope."""

    try:
        return await create_gma_workspace(
            scope_id=scope_id,
            confirmed_scope_hash=confirmed_scope_hash,
        )
    except (GmaRuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def get_workspace(workspace_id: str) -> dict[str, Any]:
    """Return one owner-bound dashboard workspace, its runs, and Action list."""

    try:
        return await get_gma_workspace(workspace_id)
    except (GmaRuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def attach_run_to_workspace(
    workspace_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Attach a canonical skill run to its matching dashboard workspace."""

    try:
        return await attach_gma_run_to_workspace(workspace_id, run_id)
    except (GmaRuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error


@gma_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def update_workspace_actions(
    workspace_id: str,
    selected_action_ids: list[str],
) -> dict[str, Any]:
    """Save exact dashboard selections; never validate or change Google Ads."""

    try:
        return await update_gma_workspace_actions(
            workspace_id,
            selected_action_ids,
        )
    except (GmaRuntimeError, ValueError) as error:
        raise ToolError(str(error)) from error
