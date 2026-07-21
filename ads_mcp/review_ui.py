"""Private bearer-link review surface for a GMA Change Plan."""

from __future__ import annotations

import html
import json
from typing import Any, Mapping

from starlette.responses import HTMLResponse

from ads_mcp.changesets import ChangesetError, get_changeset_service

SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _json(value: Any) -> str:
    return _escape(json.dumps(value, sort_keys=True, ensure_ascii=False))


def _status_text(plan: Mapping[str, Any]) -> str:
    status = plan.get("status", "draft")
    if status == "draft" and (plan.get("review_selection") or {}).get("status"):
        return "Selected for validation"
    return status.replace("_", " ").title()


def render_change_plan(
    plan: Mapping[str, Any],
    review_token: str,
    *,
    approval_token: str | None = None,
    message: str | None = None,
) -> str:
    """Render the canonical plan table and expandable evidence cards."""

    selected = set(plan.get("selected_action_ids") or [])
    status = plan.get("status")
    can_select = plan.get("run_mode") == "interactive" and status in {
        "draft",
        "validation_failed",
        "drifted",
    }
    rows = []
    details = []
    for action in plan.get("actions", []):
        action_id = action["id"]
        applyable = action.get("applyability") == "applyable"
        if applyable and can_select:
            checked = " checked" if action_id in selected else ""
            select_cell = (
                f'<input type="checkbox" name="action_id" '
                f'value="{_escape(action_id)}"{checked} '
                f'aria-label="Select {_escape(action_id)}">'
            )
        elif action_id in selected:
            select_cell = "Selected"
        else:
            select_cell = _escape(action.get("applyability", "advisory").title())
        change = f"{_json(action.get('current_value'))} → {_json(action.get('proposed_value'))}"
        rows.append(
            "<tr>"
            f"<td>{select_cell}</td>"
            f"<td><strong>{_escape(action_id)}</strong></td>"
            f"<td>{_escape(action.get('priority'))}</td>"
            f"<td>{_escape(action.get('entity'))}</td>"
            f"<td>{change}</td>"
            f"<td>{_escape(action.get('reason'))}</td>"
            f"<td>{_escape(action.get('evidence_summary'))}</td>"
            f"<td>{_escape(action.get('risk'))} / "
            f"{_escape(action.get('validation_status'))}</td>"
            "</tr>"
        )
        details.append(
            "<details>"
            f"<summary>{_escape(action_id)} — Why GMA recommends this</summary>"
            f"<p>{_escape(action.get('details'))}</p>"
            f"<p><strong>Expected direction:</strong> "
            f"{_escape(action.get('expected_impact'))}</p>"
            f"<p><strong>Evidence type:</strong> "
            f"{_escape(action.get('evidence_label'))} · "
            f"<strong>Reversible:</strong> {_escape(action.get('reversible'))}</p>"
            "</details>"
        )

    form = ""
    if can_select and any(
        action.get("applyability") == "applyable" for action in plan.get("actions", [])
    ):
        form = (
            f'<form method="post" action="/changesets/{_escape(plan["id"])}/select">'
            f'<input type="hidden" name="token" value="{_escape(review_token)}">'
            '<button type="submit">Save selected items for validation</button>'
            "</form>"
        )
    elif status == "validated":
        form = (
            '<div class="warning"><strong>Approval is not application.</strong> '
            "This approves only the exact validated rows above. Claude or Codex "
            "will still show a separate final apply prompt.</div>"
            f'<form method="post" action="/changesets/{_escape(plan["id"])}/approve">'
            f'<input type="hidden" name="token" value="{_escape(review_token)}">'
            '<button class="approve" type="submit">Approve selected items</button>'
            "</form>"
        )
    elif (plan.get("review_selection") or {}).get(
        "status"
    ) == "selected_for_validation":
        form = (
            '<div class="next"><strong>Selection saved.</strong> Return to Claude '
            "or Codex and say: “Validate my selected GMA Change Plan.” Refresh this "
            "page after validation.</div>"
        )

    token_box = ""
    if approval_token:
        token_box = (
            '<div class="token"><strong>Approved.</strong> Copy this one-time token '
            "back to Claude or Codex for the final apply request. The host will ask "
            "you once more before Google Ads can change."
            f'<input value="{_escape(approval_token)}" readonly '
            'aria-label="One-time approval token"></div>'
        )

    notice = f'<div class="notice">{_escape(message)}</div>' if message else ""
    campaign_names = (
        ", ".join(
            campaign.get("name", campaign.get("id", ""))
            for campaign in plan.get("campaigns", [])
        )
        or "All eligible campaigns"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GMA Change Plan</title>
<style>
:root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; color: #111827; background: #f3f4f6; }}
body {{ margin: 0; padding: 32px; }} main {{ max-width: 1500px; margin: auto; background: white; border-radius: 18px; padding: 28px; box-shadow: 0 8px 30px #11182714; }}
h1 {{ margin: 0 0 8px; }} .meta {{ color: #4b5563; margin-bottom: 20px; }} .status {{ display: inline-block; padding: 5px 10px; border-radius: 999px; background: #e0f2fe; color: #075985; font-weight: 700; }}
.table-wrap {{ overflow-x: auto; }} table {{ border-collapse: collapse; width: 100%; font-size: 14px; }} th, td {{ border-bottom: 1px solid #e5e7eb; padding: 12px 10px; text-align: left; vertical-align: top; }} th {{ background: #f9fafb; position: sticky; top: 0; }}
details {{ margin: 12px 0; padding: 12px 16px; border: 1px solid #e5e7eb; border-radius: 10px; }} summary {{ cursor: pointer; font-weight: 700; }}
button {{ margin-top: 20px; border: 0; border-radius: 10px; padding: 12px 18px; font-weight: 800; background: #2563eb; color: white; cursor: pointer; }} button.approve {{ background: #047857; }}
.warning, .next, .token, .notice {{ margin-top: 20px; padding: 14px 16px; border-radius: 10px; }} .warning {{ background: #fff7ed; }} .next {{ background: #eff6ff; }} .token {{ background: #ecfdf5; }} .notice {{ background: #fef3c7; }}
.token input {{ display: block; width: min(760px, 95%); margin-top: 12px; padding: 10px; font-family: ui-monospace, monospace; }}
footer {{ margin-top: 28px; color: #6b7280; font-size: 13px; }}
</style>
</head>
<body><main>
<h1>GMA Change Plan</h1>
<p class="meta"><span class="status">{_escape(_status_text(plan))}</span> · {_escape(plan.get('account_name'))} ({_escape(plan.get('customer_id'))}) · {_escape(plan.get('analysis_start'))} to {_escape(plan.get('analysis_end'))}<br>Campaigns: {_escape(campaign_names)} · Skills: {_escape(', '.join(plan.get('skills', [])))}</p>
{notice}{token_box}
<div class="table-wrap"><table>
<thead><tr><th>Select</th><th>ID</th><th>Priority</th><th>Entity</th><th>Current → proposed</th><th>Reason</th><th>Evidence summary</th><th>Risk / status</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<h2>Detailed explanation</h2>{''.join(details)}
{form}
<footer>This private bearer link expires at {_escape(plan.get('expires_at'))}. Scheduled plans are analysis-only. No recommendation, selection, or validation changes Google Ads.</footer>
</main></body></html>"""


def _response(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status_code, headers=SECURITY_HEADERS)


def _error_response(message: str, status_code: int = 400) -> HTMLResponse:
    body = (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<title>GMA Change Plan</title><body><h1>Change Plan unavailable</h1>"
        f"<p>{_escape(message)}</p></body></html>"
    )
    return _response(body, status_code)


def register_review_routes(mcp) -> None:
    @mcp.custom_route("/changesets/{changeset_id}/review", methods=["GET"])
    async def review(request):
        changeset_id = request.path_params["changeset_id"]
        token = request.query_params.get("token", "")
        try:
            plan = await get_changeset_service().get_for_review(changeset_id, token)
            return _response(render_change_plan(plan, token))
        except ChangesetError:
            return _error_response("This private link is invalid or expired.", 404)

    @mcp.custom_route("/changesets/{changeset_id}/select", methods=["POST"])
    async def select(request):
        changeset_id = request.path_params["changeset_id"]
        form = await request.form()
        token = str(form.get("token", ""))
        selected_ids = [str(value) for value in form.getlist("action_id")]
        try:
            plan = await get_changeset_service().select_for_review(
                changeset_id, token, selected_ids
            )
            return _response(
                render_change_plan(
                    plan,
                    token,
                    message="Selection saved. Validation still happens in Claude or Codex.",
                )
            )
        except ChangesetError as error:
            return _error_response(str(error), 400)

    @mcp.custom_route("/changesets/{changeset_id}/approve", methods=["POST"])
    async def approve(request):
        changeset_id = request.path_params["changeset_id"]
        form = await request.form()
        token = str(form.get("token", ""))
        try:
            result = await get_changeset_service().approve_from_review(
                changeset_id, token
            )
            plan = await get_changeset_service().get_for_review(changeset_id, token)
            return _response(
                render_change_plan(
                    plan,
                    token,
                    approval_token=result["approval_token"],
                )
            )
        except ChangesetError as error:
            return _error_response(str(error), 400)
