<!--
Template scaffolding is in English.
Planner-filled placeholders (title, goal, criteria, notes, builder/reviewer responsibilities, etc.)
MUST be written in Chinese per task_breakdown.md language rules.
-->

# {title}

---

## Metadata

| Field | Value |
| :--- | :--- |
| Agent | {agent} |
| Priority | {priority} |
| Depends on | {depends_on} |
| Risk Level | {risk_level} |
| Scope Budget | {scope_budget} |
| Owner | {owner} |

---

## Task Goal

{goal}

## In Scope

{builder_responsibilities}

## Out of Scope

{not_in_scope}

## Forbidden (Hard Boundary)

{forbidden}

## Files In Scope

{files}

## Planning Evidence

{evidence}

---

## Risk Assessment

- [ ] Touches database schema / migrations
- [ ] Touches authentication / middleware
- [ ] Touches core directory structure
- [ ] Needs Owner confirmation

## Risks & Notes

{notes}

---

## Acceptance Criteria

{criteria}

## Verification Matrix

> During execution, fill each AC row with the verification command, expected result, and evidence location.

{ac_matrix}

## Execution Order

1. Ship the minimum runnable happy path first.
2. Then handle edge cases and error branches.
3. Finally, run every check in the verification matrix.

---

## Reviewer Checkpoints

{reviewer_responsibilities}

- Only verify AC items for this task; do not expand to unrelated tech debt.
- Confirm nothing in "Forbidden" or "Out of Scope" was touched.
- Confirm the verification matrix has real evidence — verbal "verified" is not accepted.
- End with `VERDICT: PASS` or `VERDICT: FAIL`.

---

## Rollback Strategy

- **Trigger** — Any critical verification matrix item fails, or regression risk is introduced.
- **Action** — Revert newly added entry points or high-risk changes; restore the previous stable state.
- **Fallback** — Keep a minimum-viable implementation, record limitations, defer remaining work.

---

## Delivery Record

> Fill in after execution.

- **Completed at** —
- **Changed files** —
- **Verification result** —
- **API changes** — (If any endpoints were added/modified/removed, update OpenAPI on dev and sync to Apifox.)
- **Review verdict** — `VERDICT: PASS` / `VERDICT: FAIL`
- **Risks & limitations** —
- **Next steps** —
