"""Human-approved Change Plan tools; never a generic Google Ads mutate surface."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp.changesets import ChangesetError, get_changeset_service

changesets_mcp = FastMCP("changesets", mask_error_details=True)


def _tool_error(error: Exception) -> ToolError:
    return ToolError(str(error))


@changesets_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def capabilities() -> dict[str, Any]:
    """Return controlled-change feature flags and the typed operation allowlist."""

    return get_changeset_service().capabilities()


@changesets_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def create(plan: dict[str, Any]) -> dict[str, Any]:
    """Persist a bounded GMA Change Plan without changing Google Ads.

    The plan must include the confirmed Run Scope and action evidence. Only
    allowlisted typed operations may be marked applyable; every other finding
    must be advisory, blocked, hold, or monitor.
    """

    try:
        return await get_changeset_service().create(plan)
    except ChangesetError as error:
        raise _tool_error(error) from error


@changesets_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def get(changeset_id: str) -> dict[str, Any]:
    """Retrieve one unexpired Change Plan owned by the current OAuth identity."""

    try:
        return await get_changeset_service().get(changeset_id)
    except ChangesetError as error:
        raise _tool_error(error) from error


@changesets_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def validate(changeset_id: str, selected_action_ids: list[str]) -> dict[str, Any]:
    """Re-read selected current values and run Google validate_only.

    This never changes Google Ads. It rejects scheduled runs, unsupported
    actions, account drift, and invalid API mutations. The returned operation
    hash identifies the exact validated selection that may be approved.
    """

    try:
        return await get_changeset_service().validate(changeset_id, selected_action_ids)
    except ChangesetError as error:
        raise _tool_error(error) from error


@changesets_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    meta={"anthropic/requiresUserInteraction": True},
)
async def approve(
    changeset_id: str, operation_hash: str, confirmation: str
) -> dict[str, Any]:
    """Approve the exact validated selection and issue a one-time token.

    The host must ask a human every time. The confirmation phrase and operation
    hash must match the validation response exactly. This stores approval but
    does not change Google Ads.
    """

    try:
        return await get_changeset_service().approve(
            changeset_id, operation_hash, confirmation
        )
    except ChangesetError as error:
        raise _tool_error(error) from error


@changesets_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    meta={"anthropic/requiresUserInteraction": True},
)
async def apply(changeset_id: str, approval_token: str) -> dict[str, Any]:
    """Atomically apply only an approved, unchanged, allowlisted changeset.

    Live apply fails closed unless every global, developer-token, per-customer,
    ownership, expiry, validation, drift, hash, approval, and replay gate passes.
    Scheduled runs can never call this successfully.
    """

    try:
        return await get_changeset_service().apply(changeset_id, approval_token)
    except ChangesetError as error:
        raise _tool_error(error) from error


@changesets_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def verify(changeset_id: str) -> dict[str, Any]:
    """Read affected resources back and attach recent Change Event evidence."""

    try:
        return await get_changeset_service().verify(changeset_id)
    except ChangesetError as error:
        raise _tool_error(error) from error
