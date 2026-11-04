---
title: "SUBAGENT series — closure audit (criterion by criterion)"
status: Audit
version: 1.0.0
date: 2026-11-04
scope: "every §6 acceptance criterion of SUBAGENT-01 to SUBAGENT-11, matched to the test that decides it, at the tip of the SUBAGENT wave PRs"
related_prds: [SUBAGENT-01, SUBAGENT-02, SUBAGENT-03, SUBAGENT-04, SUBAGENT-05, SUBAGENT-06, SUBAGENT-07, SUBAGENT-08, SUBAGENT-09, SUBAGENT-10, SUBAGENT-11]
tags: [subagents, audit]
---

# SUBAGENT series — closure audit

Evaluated on branch `subagent-wave-7-control-remainder` (PR #13, stacked on
#7–#12), after the criteria the 0.2.0 audit in SUBAGENT-10 §3 left partial or
missing were closed by waves 1–7. Every row names the test that decides the
criterion. A file path without `::` means the criterion is a "passes
unchanged" claim about that whole file.

**Result.** 147 criteria across eleven PRDs; every one maps to a passing test
(157 distinct tests and files, 336 test cases, all passing:
`uv run pytest <every node below>`). Three criteria are superseded rather than
met as written, each by a recorded decision: SUBAGENT-06 criterion 10 and
SUBAGENT-10 criteria 21–22 (see their rows). SUBAGENT-10 criterion 26 is met
by the documents themselves: each SUBAGENT-01…11 file carries an
implementation-status section and its `status:` line.

Closures made after the 0.2.0 audit, by wave:

| Wave | PR | Closed |
|---|---|---|
| 1 | #7 | 01-2, 04-6 (D-1), 10-11/12/17/18/23/27 |
| 2 | #8 | 05-4, 05-8, 08-5, 10-13/14/19/20/25, test re-homing (10-15) |
| 3 | #9 | 06-1…11, 10-1…10/24 |
| 4 | #10 | 07-3/5/6/7, 11-2…9 |
| 5 | #11 | 07-1/8, 11-1/10/12/14/15/16, and three shipped defects |
| 6 | #12 | 09-1…14 |
| 7 | #13 | 07-4/9/10/12/13, 08-6/11/13, 01-5, 11-3/11/13/14/17/18 |


## SUBAGENT-01

Source: [SUBAGENT-01-declarative-subagent-types.md](SUBAGENT-01-declarative-subagent-types.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | A `.agenthicc/agents/<name>.md` file with valid frontmatter is discovered and spawnable by name: `pytest tests/unit/test_subagent_pool.py -q`… | `tests/unit/test_agents_registry_coverage.py::test_agents_registry_prompt_instance_and_plugin_discovery`<br>`tests/unit/test_subagent_plugin.py::test_definition_description_reaches_spawn_tool_description_and_enum` |
| 2 | The `spawn_subagents` description lists each declared type **with** its description text, and the JSON-schema enum contains the same names:… | `tests/unit/test_subagent_plugin.py::test_definition_description_reaches_spawn_tool_description_and_enum`<br>`tests/unit/test_subagent_pool.py::TestSpawnSubagentsTool::test_tool_description_lists_registered_type_descriptions` |
| 3 | A project definition overrides a user definition of the same name, and both override a built-in: `pytest tests/unit/test_subagent_pool.py -q` | `tests/unit/test_agents_registry_coverage.py::test_markdown_definition_precedence_reports_shadowed_source` |
| 4 | An invalid definition (unknown key, missing `description`, unparsable YAML) is rejected with the file path and line, and is **not** registered:… | `tests/unit/test_subagent_plugin.py::test_invalid_project_definition_does_not_fall_back_to_builtin_type`<br>`tests/unit/test_subagent_permissions.py::test_definition_permissions_reject_unknown_keys_and_capabilities` |
| 5 | `SubagentTypeSpec(...).description` exists and defaults to `""` for the existing plugin path, so no current plugin breaks: `pytest… | `tests/unit/test_subagent_plugin.py::test_a_plugin_spec_without_a_description_defaults_to_empty` |
| 6 | A plugin exporting `SUBAGENT_TYPES` still registers its type: `pytest tests/unit/test_subagent_plugin.py -q` | `tests/unit/test_subagent_plugin.py::TestPluginSubagentTypesLoading::test_load_plugin_file_with_subagent_types` |
| 7 | A type named `research` is accepted as a real name and no longer collides with the `research` → `researcher` alias… | `tests/unit/test_subagent_pool.py::TestSpawnSubagentsTool::test_research_is_a_distinct_registered_type`<br>`tests/unit/test_subagent_pool.py::TestSpawnSubagentsTool::test_tool_schema_advertises_builtin_names_without_research_alias` |
| 8 | `pytest tests/integration/test_subagent_tool_execution.py -q` passes unchanged, proving the tool boundary did not shift | `tests/integration/test_subagent_tool_execution.py` |
| 9 | A shadowed definition is reported with both sources, and two definitions of the same name inside one directory are refused naming both files:… | `tests/unit/test_agents_registry_coverage.py::test_markdown_definition_precedence_reports_shadowed_source`<br>`tests/unit/test_agents_registry_coverage.py::test_duplicate_markdown_names_are_invalid_and_name_both_files` |
| 10 | A project definition that shadows a built-in without `override: true` is refused with the built-in named; with the key set the project definition… | `tests/unit/test_subagent_plugin.py::test_project_definition_must_opt_in_to_shadow_builtin_subagent`<br>`tests/unit/test_subagent_plugin.py::test_project_definition_can_opt_in_to_shadow_builtin_subagent` |

## SUBAGENT-02

Source: [SUBAGENT-02-per-type-model-and-context-routing.md](SUBAGENT-02-per-type-model-and-context-routing.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | With no per-type, operator, or call routing settings, a worker is decorated with the parent model and built with… | `tests/unit/test_subagent_pool.py::TestSubagentWorkerRouting::test_defaults_inherit_parent_model_and_legacy_memory_budget` |
| 2 | A definition declaring `model: <id>` produces workers decorated with that id and usage records naming it: new cases in | `tests/unit/test_subagent_pool.py::TestSubagentWorkerRouting::test_type_model_effort_and_context_are_applied_and_context_is_clamped` |
| 3 | An omitted type model uses the operator default when configured and otherwise the parent; `model: inherit` always selects the parent even when an… | `tests/unit/test_subagent_pool.py::TestSubagentWorkerRouting::test_operator_routing_defaults_and_explicit_model_inherit` |
| 4 | A definition declaring `effort:` yields that value in the worker's resolved provider options; with no type or operator effort, the inherited… | `tests/unit/test_subagent_pool.py::TestSubagentWorkerRouting::test_type_model_effort_and_context_are_applied_and_context_is_clamped`<br>`tests/unit/test_subagent_pool.py::TestSubagentWorkerRouting::test_unsupported_effort_retries_once_without_only_that_option` |
| 5 | A definition declaring `context: N` produces `ShortTermMemory(max_tokens=N)` when `N` fits the model's window, and the clamped window value when… | `tests/unit/test_subagent_pool.py::TestSubagentWorkerRouting::test_type_model_effort_and_context_are_applied_and_context_is_clamped` |
| 6 | Per-call model/effort arguments on `spawn_subagents` are rejected unless the operator-enabled switch is on, and the rejection names PRD-197 | `tests/unit/test_subagent_pool.py::TestSpawnSubagentsTool::test_per_call_model_override_is_rejected_by_default`<br>`tests/unit/test_subagent_pool.py::TestSpawnSubagentsTool::test_call_override_schema_is_hidden_until_operator_enables_it` |
| 7 | The usage record for a worker carries the effective model rather than `self._parent_model`: extended cases in | `tests/integration/test_usage_ledger_integration.py::test_standard_compaction_and_subagent_paths_use_the_same_ledger` |
| 8 | A malformed model identifier or unsupported effort is rejected at definition parse time with the file path and offending key: . An invalid catalog… | `tests/unit/test_agent_library.py::test_definition_rejects_invalid_model_identifier_with_source_location`<br>`tests/unit/test_agent_library.py::test_definition_rejects_invalid_effort_with_source_location`<br>`tests/unit/test_subagent_plugin.py::test_invalid_project_definition_does_not_fall_back_to_builtin_type` |
| 9 | `pytest tests/integration/test_subagent_tool_execution.py -q` and `pytest tests/integration/test_subagent_executor_integration.py -q` pass… | `tests/integration/test_subagent_tool_execution.py`<br>`tests/integration/test_subagent_executor_integration.py` |

## SUBAGENT-03

Source: [SUBAGENT-03-structured-result-contracts.md](SUBAGENT-03-structured-result-contracts.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | A type with no `output:` contract behaves exactly as today, including the tool-call-summary fallback marking a silent worker successful: existing… | `tests/unit/test_subagent_pool.py`<br>`tests/unit/test_subagent_pool_runtime_coverage.py` |
| 2 | A type declaring `output:` causes the worker's tool set to include a submission tool whose input schema is the declared schema, and omits it for… | `tests/integration/test_subagent_tool_execution.py::test_structured_result_submission_uses_contract_schema_and_returns_value`<br>`tests/unit/test_subagent_pool.py::TestSubagentTypeSpec::test_output_schema_is_normalized_on_type_registration` |
| 3 | A valid submission populates `SubagentResult.value` and the rendered `text` contains the rendered value | `tests/integration/test_subagent_tool_execution.py::test_structured_result_submission_uses_contract_schema_and_returns_value` |
| 4 | A submission that violates the schema yields `ok=False` with an error naming the field, and the pool counts it as failed rather than succeeded | `tests/integration/test_subagent_tool_execution.py::test_structured_result_validation_failure_is_typed_and_counts_as_failure` |
| 5 | A worker for a contract-declaring type that finishes without submitting yields `ok=False` — the same shape as the existing `implementer` rule at… | `tests/integration/test_subagent_tool_execution.py::test_contract_worker_without_submission_fails` |
| 6 | A renderer that raises produces a failed result with a distinct error code and does not raise out of `SubagentPool.run()` | `tests/integration/test_subagent_tool_execution.py::test_renderer_failure_is_contained_as_a_failed_worker`<br>`tests/integration/test_subagent_tool_execution.py::test_structured_result_projector_failure_is_typed_and_counts_as_failure` |
| 7 | An invalid `output:` schema is refused at definition-load time with the file path and the offending key, and the type is not registered | `tests/unit/test_subagent_plugin.py::test_invalid_output_schema_is_reported_and_not_registered`<br>`tests/unit/test_subagent_pool.py::TestSubagentTypeSpec::test_invalid_output_schema_is_rejected_on_type_registration` |
| 8 | Two tasks whose types declare different contracts do not share a cached result, and the fingerprint changes when the schema changes | `tests/unit/test_subagent_pool.py::TestSpawnSubagentsTool::test_output_schema_changes_the_effective_task_fingerprint` |
| 9 | `pytest tests/integration/test_subagent_tool_execution.py -q` and `pytest tests/integration/test_subagent_executor_integration.py -q` pass… | `tests/integration/test_subagent_tool_execution.py`<br>`tests/integration/test_subagent_executor_integration.py` |

## SUBAGENT-04

Source: [SUBAGENT-04-nesting-depth-and-fan-out.md](SUBAGENT-04-nesting-depth-and-fan-out.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | A pool constructed with a child depth greater than `max_depth` is refused with an error naming the current depth, the limit and the setting that… | `tests/unit/test_subagent_pool.py::TestSubagentPoolGovernance::test_depth_over_limit_is_refused_before_worker_construction` |
| 2 | With `max_depth=1` (the default) every built-in type's worker tool set excludes `spawn_subagents`, asserted directly rather than inferred from a grep | `tests/unit/test_subagent_pool.py::TestSubagentWorkerRouting::test_builtin_workers_never_expose_spawn_tool_at_default_depth` |
| 3 | With `max_depth=2`, a `max_depth=1` worker's tool set still excludes `spawn_subagents`, and a worker one level below the limit has it available —… | `tests/unit/test_subagent_pool.py::TestSubagentWorkerRouting::test_spawn_tool_visibility_tracks_worker_depth` |
| 4 | A synthetic type whose `allowed_tools` includes `spawn_subagents` still cannot delegate at `max_depth=1`: the pool refuses to construct a child,… | `tests/integration/test_subagent_tool_execution.py::test_synthetic_delegating_type_cannot_spawn_past_max_depth` |
| 5 | A task list longer than the declared maximum is rejected before any `SubagentWorker` is constructed, and the error states the limit and the… | `tests/unit/test_subagent_pool.py::TestSubagentPoolGovernance::test_depth_specific_task_limit_is_refused_before_worker_construction`<br>`tests/unit/test_subagent_budgets.py::test_spawn_tool_rejects_task_lists_over_worker_limit` |
| 6 | *(Amended 2026-11-03 by SUBAGENT-10 D-1.)* A `max_concurrent` argument above the declared ceiling is refused before any worker starts, and the… | `tests/integration/test_subagent_tool_execution.py::test_nested_depth_tier_refuses_concurrency_above_inherited_limit`<br>`tests/unit/test_subagent_budgets.py::test_spawn_tool_rejects_concurrency_over_configured_limit`<br>`tests/integration/test_subagent_tool_execution.py::test_concurrency_below_one_is_refused_without_reporting_a_used_value` |
| 7 | Exceeding the pool-count limit is refused with an error naming the limit and the pools currently running | `tests/unit/test_subagent_pool.py::TestSubagentPoolGovernance::test_process_wide_pool_limit_reports_active_pools` |
| 8 | A child pool whose `(type, task)` signature repeats in its ancestry is refused, and the error prints the ancestry chain | `tests/unit/test_subagent_pool.py::TestSubagentPoolGovernance::test_repeated_task_signature_is_refused_with_ancestry_chain` |
| 9 | `WorkerState` and `SubagentPoolState` carry depth and parent pool id, the panel renders depth for a nested pool, and the flat-pool rendering is… | `tests/unit/test_subagent_panel_agx08.py::test_nested_pool_summary_shows_depth_and_keeps_parent_identity`<br>`tests/unit/test_subagent_panel_agx08.py::test_the_panel_lists_each_workers_status_activity_time_and_tokens` |
| 10 | A depth-tier policy that tries to grant a model or tool the type does not declare is refused at load, and one that narrows is accepted | `tests/unit/test_subagent_plugin.py::test_depth_tier_narrowing_is_accepted_and_widening_is_rejected`<br>`tests/unit/test_subagent_pool.py::TestSubagentWorkerRouting::test_depth_tier_narrows_worker_tools_and_models` |
| 11 | `pytest tests/integration/test_subagent_tool_execution.py -q` and `pytest tests/unit/test_subagent_pool.py -q` pass unchanged, proving the default… | `tests/integration/test_subagent_tool_execution.py`<br>`tests/unit/test_subagent_pool.py` |

## SUBAGENT-05

Source: [SUBAGENT-05-per-type-permission-narrowing.md](SUBAGENT-05-per-type-permission-narrowing.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | A type with no `permissions:` block yields an effective scope equal to the inherited one and an unchanged `policy_revision`, so the default path… | `tests/unit/test_subagent_permissions.py::test_permission_changes_change_revision_but_absence_is_stable`<br>`tests/unit/test_subagent_permissions.py::test_agent_spec_with_no_permission_block_remains_optional` |
| 2 | A `deny_tools` pattern blocks a tool that the same type's `allowed_tools` permits, proving denies take precedence over allows | `tests/unit/test_subagent_permissions.py::test_tool_scope_denials_apply_wildcards_after_allow_list` |
| 3 | A wildcard deny (`outlook_*`) blocks every matching tool, including one registered after the type was defined, which is the registry-growth case… | `tests/unit/test_subagent_permissions.py::test_worker_excludes_later_tools_matching_type_deny` |
| 4 | A `deny_capabilities` entry removes that capability from the worker's `RuntimeMode`, and a tool carrying only a denied capability is refused by… | `tests/unit/test_subagent_pool.py::test_worker_refuses_a_denied_capability_tool_through_its_own_gate` |
| 5 | A `require_approval` entry causes a tool the parent mode allowed without approval to require approval for that type only, while a sibling type… | `tests/unit/test_subagent_permissions.py::test_approval_constraint_is_local_to_its_type` |
| 6 | A `workspace_paths` entry narrows `allowed_roots` to a strict subset, and a path outside the inherited scope is refused at definition load with… | `tests/unit/test_subagent_permissions.py::test_workspace_scope_restriction_is_a_subset_and_empty_scope_denies_all`<br>`tests/unit/test_subagent_permissions.py::test_definition_registry_rejects_out_of_scope_workspace_paths` |
| 7 | Attempting to widen — a tool outside the inherited ceiling, or a capability the parent blocks, or clearing an inherited deny — is refused at load… | `tests/unit/test_subagent_permissions.py::test_definition_permissions_reject_unknown_keys_and_capabilities`<br>`tests/unit/test_subagent_plugin.py::test_depth_tier_narrowing_is_accepted_and_widening_is_rejected` |
| 8 | The effective scope equals `inherited.restrict(declared)` for a table of declarations, asserted against the existing implementation rather than a… | `tests/unit/test_agent_permissions.py::TestSubagentPermissionMergeTable::test_effective_scope_is_the_restriction_of_the_declaration` |
| 9 | A definition whose `permissions:` block changes produces a different `policy_revision`, so a cached result made under weaker constraints cannot be… | `tests/unit/test_subagent_permissions.py::test_permission_changes_change_revision_but_absence_is_stable` |
| 10 | `pytest tests/unit/test_agent_permissions.py -q` and `pytest tests/integration/test_subagent_tool_execution.py -q` pass unchanged, proving the… | `tests/unit/test_agent_permissions.py`<br>`tests/integration/test_subagent_tool_execution.py` |
| 11 | A capability denied by the type aborts the call with an error that names the type and the constraint and offers no mode switch, while a tool… | `tests/unit/test_subagent_permissions.py::test_type_capability_denial_reaches_runtime_gate_without_widening_advice`<br>`tests/unit/test_subagent_pool.py::test_a_type_declared_policy_narrows_the_first_attempt` |

## SUBAGENT-06

Source: [SUBAGENT-06-pool-budgets-and-limits.md](SUBAGENT-06-pool-budgets-and-limits.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | A pool's token total equals the sum of its workers' ledger records and the session total is unchanged by the aggregation, proving the pool number… | `tests/unit/test_usage_ledger.py::test_pool_total_is_the_sum_of_its_workers_records_and_a_subset_of_the_session` |
| 2 | Two `spawn_subagents` calls in one parent run produce disjoint pool totals, which fails today because both write `…:subagent:task-0` | `tests/unit/test_subagent_pool.py::test_two_pools_in_one_parent_run_are_accounted_separately`<br>`tests/unit/test_usage_ledger.py::test_two_pools_of_one_parent_run_have_disjoint_totals` |
| 3 | A pool whose records carry `cost_status="unavailable"` reports cost as unknown rather than `$0.00`, and a `cost_usd` budget on such a pool is… | `tests/unit/test_usage_ledger.py::test_unknown_cost_is_unavailable_not_zero`<br>`tests/integration/test_subagent_tool_execution.py::test_a_cost_budget_is_refused_for_an_unpriced_model_or_without_a_ledger` |
| 4 | Provisional records are excluded from the pool total, so the total is monotone across snapshots taken while workers run and does not double-count… | `tests/unit/test_usage_ledger.py::test_provisional_calls_are_excluded_so_the_total_is_monotone` |
| 5 | `spawn_subagents(budget={"tokens": 1000})` lowers the effective limit, and a call declaring more than `subagents.max_budget` is clamped to the… | `tests/unit/test_subagent_budgets.py::test_declaration_lowers_operator_limits_and_never_raises_them`<br>`tests/integration/test_subagent_tool_execution.py::test_a_declaration_above_the_settings_ceiling_is_clamped_and_reported` |
| 6 | A malformed budget — zero, negative, non-integer tokens, non-finite cost — is refused before any worker starts, naming the offending field, and a… | `tests/integration/test_subagent_tool_execution.py::test_malformed_budget_is_refused_before_any_provider_call`<br>`tests/unit/test_subagent_budgets.py::test_malformed_budget_is_refused_naming_the_field`<br>`tests/integration/test_subagent_tool_execution.py::test_no_budget_keeps_the_digest_unchanged_and_still_reports_usage` |
| 7 | Each `SubagentResult` reports its own tokens and duration, and `AggregatedResult` reports the pool total, so a pool of two unequal workers… | `tests/unit/test_subagent_pool.py::test_each_worker_reports_its_own_spend_and_the_pool_reports_the_total` |
| 8 | The `SubagentPoolCompleted` payload and the tool's text digest both carry usage-versus-limit, and the text says *crossed* when a declared limit… | `tests/integration/test_subagent_tool_execution.py::test_pool_completed_event_carries_usage_against_limits`<br>`tests/integration/test_subagent_tool_execution.py::test_crossing_a_declared_token_limit_is_reported_with_its_source` |
| 9 | `SubagentPoolState` publishes the pool's tokens, cost and declared limit, and the panel renders them through PRD-157's existing signals without a… | `tests/unit/test_subagent_panel_agx08.py::test_a_declared_budget_is_shown_as_usage_against_it`<br>`tests/unit/test_subagent_panel_agx08.py::test_a_pool_without_a_declared_budget_renders_as_before` |
| 10 | Crossing a declared limit produces a recorded crossing and no cancellation: the worker count, statuses and returned text are identical to the same… *(superseded: superseded by D-2)* | `tests/integration/test_subagent_tool_execution.py::test_crossing_a_declared_token_limit_is_reported_with_its_source`<br>`tests/unit/test_subagent_budgets.py::test_count_budget_crossings_apply_drain_policy` |
| 11 | `pytest tests/unit/test_usage_ledger.py -q` and `pytest tests/unit/test_subagent_pool.py -q` pass unchanged for declarations that are absent,… | `tests/unit/test_subagent_budgets.py::test_no_declaration_and_no_crossing_adds_no_digest`<br>`tests/unit/test_subagent_pool_runtime_coverage.py` |

## SUBAGENT-07

Source: [SUBAGENT-07-background-orchestration.md](SUBAGENT-07-background-orchestration.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | `spawn_subagents(..., background=True)` returns within one second with `status="running"` and a `pool_id`, while its workers are still running,… | `tests/integration/test_subagent_tool_execution.py::test_a_model_requested_background_pool_returns_running_at_once` |
| 2 | A ledger record exists with `state="running"` and one row per worker *before* the first worker's first turn, and it is readable from the journal alone | `tests/unit/test_conversation_journal.py::TestDurabilityAndCrash::test_background_pool_recovery_is_durable_and_does_not_rerun`<br>`tests/integration/test_subagent_tool_execution.py::test_a_model_requested_background_pool_returns_running_at_once` |
| 3 | `collect_subagent_results(pool_id)` on a running pool returns `status="running"` with per-worker rows and does not block the parent turn | `tests/unit/test_subagent_resume.py::TestCollectSubagentResults::test_a_running_pool_returns_a_snapshot_without_blocking` |
| 4 | `collect_subagent_results(pool_id, wait=True)` on a running pool returns the aggregate when the pool finishes, preserving today's join for… | `tests/integration/test_subagent_tool_execution.py::test_two_background_pools_collect_independently_and_stay_on_the_panel` |
| 5 | Collecting a finished pool twice returns the same aggregate both times, and the second call does not raise `unknown or expired subagent pool` | `tests/unit/test_subagent_resume.py::TestCollectSubagentResults::test_a_finished_pool_is_collected_identically_every_time` |
| 6 | A pool that finishes while no turn is waiting is delivered exactly once: the next turn receives one system event naming the pool id, one `ok`… | `tests/unit/test_tui_session_coverage.py::test_completed_background_subagent_is_delivered_once`<br>`tests/unit/test_subagent_resume.py::TestBackgroundPoolDelivery::test_only_the_runtime_notice_enters_the_user_role` |
| 7 | After a simulated restart — a fresh registry and no live pool — a record previously `running` reads `orphaned`, keeps its worker records, and its… | `tests/unit/test_subagent_resume.py::TestCollectSubagentResults::test_after_a_restart_a_pool_is_read_from_the_journal`<br>`tests/unit/test_conversation_journal.py::TestDurabilityAndCrash::test_background_pool_recovery_is_durable_and_does_not_rerun` |
| 8 | A background pool whose worker asks the parent with `subagents.on_clarification="fail"` ends that worker with `unanswerable_in_background` in… | `tests/integration/test_subagent_tool_execution.py::test_on_clarification_fail_ends_a_backgrounded_workers_wait_at_once`<br>`tests/unit/test_subagent_communication.py::test_a_backgrounded_workers_question_is_announced_once_without_its_text` |
| 9 | A background pool appears on the panel while running, without the panel being told about a new event kind | `tests/integration/test_subagent_tool_execution.py::test_two_background_pools_collect_independently_and_stay_on_the_panel`<br>`tests/integration/test_subagent_tool_execution.py::test_ctrl_b_backgrounds_a_pool_and_keeps_it_session_owned` |
| 10 | A restored session lists its pools, including orphaned ones, from the ledger rather than only from transcripts | `tests/unit/test_subagent_panel_agx08.py::test_a_restored_session_lists_its_pools_from_the_journal` |
| 11 | Session shutdown leaves no record in `running`: every background pool is terminal, and the terminal state was written before the shutdown completed | `tests/integration/test_subagent_tool_execution.py::test_session_shutdown_cancels_a_backgrounded_pool` |
| 12 | `background=False` produces the same aggregate text, event sequence and transcript files as the current implementation | `tests/unit/test_subagent_pool_runtime_coverage.py::test_a_backgrounded_pool_produces_the_same_aggregate_events_and_records` |
| 13 | Two background pools from the same session collect independently, in either order, with no cross-talk in results or worker ids: . ### Test… | `tests/integration/test_subagent_tool_execution.py::test_two_background_pools_collect_independently_and_stay_on_the_panel` |

## SUBAGENT-08

Source: [SUBAGENT-08-worker-lifecycle-controls.md](SUBAGENT-08-worker-lifecycle-controls.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | A stop request returns a record with a request id, mode, source reason and phase `requested`, and repeating it returns the same record rather than… | `tests/unit/test_subagent_lifecycle.py::test_abort_salvages_partial_output_and_is_idempotent` |
| 2 | An `abort` stop of a running worker ends with phase `cancelled`, the panel row status `stopped`, and the sibling workers untouched | `tests/integration/test_subagent_panel_agx08.py::test_stopping_one_worker_leaves_the_others_running` |
| 3 | **Partial-result preservation on abort:** a worker stopped after a completed provider turn returns that turn's text with `partial=True` and… | `tests/unit/test_subagent_lifecycle.py::test_abort_salvages_partial_output_and_is_idempotent` |
| 4 | A `drain` stop never starts a pending worker, and the report names the workers that never ran | `tests/unit/test_subagent_lifecycle.py::test_drain_finishes_running_worker_and_does_not_start_queued_worker` |
| 5 | A stop while the worker waits for an approval leaves no answerable prompt for that worker and does not disturb another worker's approval | `tests/unit/test_subagent_lifecycle.py::test_stopping_a_worker_waiting_on_approval_withdraws_only_its_prompt` |
| 6 | A steering message that the worker consumed is acknowledged, and one left unconsumed at termination is counted in `undelivered_messages` | `tests/unit/test_subagent_communication.py::test_a_consumed_message_is_acknowledged_and_an_unconsumed_one_is_counted` |
| 7 | `retry_worker` on a failed worker starts attempt 2 and leaves every succeeded worker's result and attempt number unchanged | `tests/unit/test_subagent_lifecycle.py::test_retry_reruns_only_failed_worker_and_records_attempt_history` |
| 8 | `retry_worker` refuses a live worker and a succeeded one, and leaves the lifecycle phase unchanged | `tests/unit/test_subagent_lifecycle.py::test_retry_refuses_live_and_succeeded_workers_without_changing_lifecycle` |
| 9 | A pool-level stop applies one mode to every worker and reports one outcome per worker; a parent-turn cancellation reaches the same code path with… | `tests/unit/test_subagent_lifecycle.py::test_parent_cancellation_requests_abort_and_waits_for_worker_cleanup`<br>`tests/unit/test_subagent_lifecycle.py::test_the_parent_model_drains_a_pool_and_learns_which_workers_never_start` |
| 10 | A budget crossing record with `source="budget"` produces a `drain` by default and an `abort` when `subagents.on_budget_exceeded = "abort"`, and… | `tests/unit/test_subagent_budgets.py::test_count_budget_crossings_apply_drain_policy`<br>`tests/unit/test_subagent_budgets.py::test_time_budget_abort_cancels_worker_and_preserves_partial_output`<br>`tests/unit/test_subagent_budgets.py::test_time_budget_drain_allows_running_worker_to_finish` |
| 11 | `dismiss_worker` accepts a stopped row as well as a failed one, and the panel action removes it | `tests/unit/test_subagent_lifecycle.py::test_a_stopped_row_can_be_dismissed_like_a_failed_one`<br>`tests/unit/test_subagent_panel_agx08.py::test_dismissing_a_finished_pools_failure_updates_the_panel` |
| 12 | A pool with no stop request produces the same text, events and transcripts as the current implementation | `tests/unit/test_subagent_pool_runtime_coverage.py::test_real_pool_run_emits_state_and_normalizes_results`<br>`tests/integration/test_subagent_panel_agx08.py::test_provider_requests_and_the_result_are_the_same_without_the_panel` |
| 13 | A worker that fails after a productive provider turn returns that turn's text with `partial=True` and a `stop_reason` naming the failure, and the… | `tests/unit/test_subagent_lifecycle.py::test_a_worker_failing_after_a_productive_turn_keeps_that_turns_text` |

## SUBAGENT-09

Source: [SUBAGENT-09-observability-and-transcript-contract.md](SUBAGENT-09-observability-and-transcript-contract.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | Every subagent emission site constructs one `SubagentEvent`, and a single schema test asserts its required fields, its `schema_version` and its… | `tests/unit/test_subagent_pool.py::test_every_subagent_record_carries_the_contract_envelope` |
| 2 | The lifecycle table names every kind the four sinks can produce, and a test fails if a sink emits a kind the table does not map | `tests/unit/test_subagent_pool_runtime_coverage.py::test_the_lifecycle_table_maps_every_kind_once`<br>`tests/unit/test_subagent_pool_runtime_coverage.py::test_every_kind_a_real_pool_emits_is_in_the_lifecycle_table`<br>`tests/unit/test_kernel_event_contract.py::test_every_emit_site_has_a_statically_known_event_type` |
| 3 | A `Subagent*` kernel event's `to_dict()` contains `schema_version == 1`, and `Event.create` without the argument still produces a valid record | `tests/unit/test_subagent_pool.py::test_an_event_without_a_version_argument_stays_valid` |
| 4 | Every subagent scroll payload and kernel payload carries `pool_id`, and every worker-scoped one carries `worker_id` | `tests/unit/test_subagent_pool.py::test_every_subagent_record_carries_the_contract_envelope` |
| 5 | A transcript line carries `pool_id` and `worker_id`, so a copied `.jsonl` is still attributable | `tests/unit/test_subagent_panel_agx08.py::test_a_transcript_line_names_its_pool_and_worker` |
| 6 | A failed worker emits `subagent_worker_failed` and a successful one emits `subagent_worker_done`, and the two differ only in `kind` and `ok` for… | `tests/unit/test_subagent_pool.py::test_success_and_failure_differ_only_in_kind_and_ok`<br>`tests/unit/test_subagent_tui.py::TestScrollBufferRenderers::test_worker_done_failure_shows_error` |
| 7 | A worker whose text exceeds the display bound produces a payload with `truncated: true` and `text_length` equal to the untruncated length, and a… | `tests/unit/test_subagent_pool.py::test_long_worker_text_is_cut_visibly_and_short_text_is_not` |
| 8 | A worker answer containing a known secret pattern is masked in the kernel payload, the scroll payload, the transcript line and the panel activity,… | `tests/unit/test_subagent_pool.py::test_worker_secret_is_masked_identically_in_every_sink`<br>`tests/unit/test_subagent_pool.py::test_a_worker_secret_is_masked_in_the_panel_activity_too` |
| 9 | Redaction is idempotent: applying it to already-redacted text changes nothing, which is what makes panel-then-transcript double application safe | `tests/unit/test_subagent_panel_agx08.py::test_redaction_is_idempotent_from_panel_to_transcript`<br>`tests/unit/test_subagent_pool.py::test_display_redaction_is_idempotent` |
| 10 | `replay_pool(pool_id)` returns one ordered timeline merging transcript and journal events for every worker, and its documented fidelity statement… | `tests/unit/test_subagent_panel_agx08.py::test_replay_pool_merges_transcripts_and_journal_records_in_order`<br>`tests/unit/test_subagent_panel_agx08.py::test_replay_marks_a_compacted_transcript_partial` |
| 11 | `stuck_workers` counts a worker `running` past the declared fraction of its timeout and does not count a `waiting` one, and the value appears on… | `tests/unit/test_subagent_pool.py::test_stuck_workers_counts_long_running_workers_but_not_waiting_ones`<br>`tests/unit/test_subagent_pool.py::test_the_pool_snapshot_and_completion_report_stuck_workers` |
| 12 | A transcript write failure increments `transcript_write_failures` and emits one `subagent_transcript_degraded` per pool, and the worker still… | `tests/unit/test_subagent_panel_agx08.py::test_a_failed_transcript_write_is_counted_and_reported_once` |
| 13 | The panel renders a pool from a `SubagentPoolState` produced by a real pool, not a hand-built payload, for at least the started, running and… | `tests/integration/test_subagent_panel_agx08.py::test_each_row_shows_its_workers_tool_before_the_tool_runs` |
| 14 | A pool with no failure produces byte-identical aggregate text and the same set of event kinds as today, plus the new fields | `tests/unit/test_subagent_pool_runtime_coverage.py::test_real_pool_run_emits_state_and_normalizes_results` |

## SUBAGENT-10

Source: [SUBAGENT-10-gap-remediation.md](SUBAGENT-10-gap-remediation.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | A pool's token total equals the sum of its workers' ledger records and the session total is unchanged by the aggregation | `tests/unit/test_usage_ledger.py::test_pool_total_is_the_sum_of_its_workers_records_and_a_subset_of_the_session` |
| 2 | Two `spawn_subagents` calls in one parent run produce disjoint pool totals, because the run id carries the pool id | `tests/unit/test_subagent_pool.py::test_two_pools_in_one_parent_run_are_accounted_separately`<br>`tests/unit/test_usage_ledger.py::test_two_pools_of_one_parent_run_have_disjoint_totals` |
| 3 | A pool whose records carry `cost_status="unavailable"` reports cost as unknown rather than `$0.00`, and declaring a `cost_usd` budget against it… | `tests/unit/test_usage_ledger.py::test_unknown_cost_is_unavailable_not_zero`<br>`tests/integration/test_subagent_tool_execution.py::test_a_cost_budget_is_refused_for_an_unpriced_model_or_without_a_ledger` |
| 4 | Provisional records are excluded, so a total taken mid-run is monotone and a reconciled call is not counted twice | `tests/unit/test_usage_ledger.py::test_provisional_calls_are_excluded_so_the_total_is_monotone` |
| 5 | `spawn_subagents(budget={"tokens": 1000})` lowers the effective limit; a declaration above `subagents.max_budget` is clamped to the ceiling and… | `tests/unit/test_subagent_budgets.py::test_declaration_lowers_operator_limits_and_never_raises_them`<br>`tests/integration/test_subagent_tool_execution.py::test_a_declaration_above_the_settings_ceiling_is_clamped_and_reported` |
| 6 | A malformed budget is refused before any worker starts, naming the field, and a call with no `budget:` behaves exactly as today | `tests/integration/test_subagent_tool_execution.py::test_malformed_budget_is_refused_before_any_provider_call`<br>`tests/unit/test_subagent_budgets.py::test_malformed_budget_is_refused_naming_the_field`<br>`tests/integration/test_subagent_tool_execution.py::test_no_budget_keeps_the_digest_unchanged_and_still_reports_usage` |
| 7 | Each `SubagentResult` reports its own tokens and duration and `AggregatedResult` the pool total, so a pool of two unequal workers attributes spend… | `tests/unit/test_subagent_pool.py::test_each_worker_reports_its_own_spend_and_the_pool_reports_the_total` |
| 8 | The `SubagentPoolCompleted` payload and the tool's text digest both carry usage-versus-limit, and the text says *crossed* — naming the limit and… | `tests/integration/test_subagent_tool_execution.py::test_pool_completed_event_carries_usage_against_limits`<br>`tests/integration/test_subagent_tool_execution.py::test_crossing_a_declared_token_limit_is_reported_with_its_source` |
| 9 | `SubagentPoolState` publishes the pool's tokens, cost and limit, and the panel renders them through PRD-157's existing signals | `tests/unit/test_subagent_panel_agx08.py::test_a_declared_budget_is_shown_as_usage_against_it`<br>`tests/unit/test_subagent_panel_agx08.py::test_a_pool_without_a_declared_budget_renders_as_before` |
| 10 | *(Amended in wave 3.)* Crossing a *declared* `budget:` limit is answered exactly like crossing an operator ceiling — by `on_budget_exceeded` — and… | `tests/integration/test_subagent_tool_execution.py::test_crossing_a_declared_token_limit_is_reported_with_its_source`<br>`tests/unit/test_subagent_budgets.py::test_report_names_the_crossed_limit_and_its_source` |
| 11 | A definition-loaded type appears in the spawn tool's description with its own description text, not "No description provided" | `tests/unit/test_subagent_plugin.py::test_definition_description_reaches_spawn_tool_description_and_enum` |
| 12 | SUBAGENT-04 criterion 6 states refusal (D-1), `tool.py:335-343` refuses, and `test_nested_depth_tier_refuses_concurrency_above_inherited_limit`… | `tests/integration/test_subagent_tool_execution.py::test_nested_depth_tier_refuses_concurrency_above_inherited_limit` |
| 13 | The `inherited.restrict(declared)` merge table exists in and asserts against `AgentCapabilityScope.restrict()` rather than a reimplementation | `tests/unit/test_agent_permissions.py::TestSubagentPermissionMergeTable::test_effective_scope_is_the_restriction_of_the_declaration` |
| 14 | A worker whose filtered tool list still contains a denied-capability tool is refused by the worker's own `ToolCapabilityGate`, asserted through… | `tests/unit/test_subagent_pool.py::test_worker_refuses_a_denied_capability_tool_through_its_own_gate` |
| 15 | Every case named by a §6 criterion of SUBAGENT-04, -05, -07 and -08 lives in the file that criterion names, and every "passes unchanged" claim is… | `tests/unit/test_agent_permissions.py`<br>`tests/unit/test_subagent_permissions.py`<br>`tests/unit/test_subagent_lifecycle.py`<br>`tests/unit/test_subagent_panel_agx08.py` |
| 16 | `pytest tests/unit/test_usage_ledger.py tests/unit/test_subagent_pool.py tests/unit/test_subagent_plugin.py tests/unit/test_agent_permissions.py… | `tests/unit/test_usage_ledger.py`<br>`tests/unit/test_subagent_pool.py`<br>`tests/unit/test_subagent_plugin.py`<br>`tests/unit/test_agent_permissions.py`<br>`tests/unit/test_subagent_permissions.py`<br>`tests/unit/test_subagent_budgets.py` |
| 17 | **B-3:** a worker answer containing a secret the session-export redactor masks is masked identically in the `subagent_worker_done` scroll payload,… | `tests/unit/test_subagent_pool.py::test_worker_secret_is_masked_identically_in_every_sink` |
| 18 | **B-3:** redaction is idempotent — applying the helper to its own output changes nothing | `tests/unit/test_subagent_pool.py::test_display_redaction_is_idempotent` |
| 19 | **B-1:** a backgrounded pool stopped by `max_tokens` with `drain` journals a `subagent_pool_stopped` record carrying non-zero `total`, the… | `tests/unit/test_subagent_budgets.py::test_budget_drained_pool_journals_a_stopped_record_with_its_results`<br>`tests/unit/test_conversation_journal.py::TestSubagentPoolTerminalRecords::test_budget_stopped_pool_folds_to_stopped_with_counts_text_and_reason` |
| 20 | **B-1:** delivering that record tells the parent the pool was stopped by the pool budget, names the reason, reports the real counts and includes… | `tests/unit/test_subagent_resume.py::TestBackgroundPoolNotice::test_budget_stop_names_the_reason_and_counts_and_asks_for_a_collect`<br>`tests/unit/test_tui_session_coverage.py::test_budget_stopped_background_subagent_is_delivered_with_its_cause`<br>`tests/unit/test_subagent_resume.py::TestBackgroundPoolNotice::test_session_close_still_reads_as_a_cancellation` |
| 21 | **B-2:** *(amended in wave 2, see goal 7)* the delivered background message carries worker text redacted and bounded, and an oversized aggregate… *(superseded: superseded by SUBAGENT-11 goal 5)* | `tests/unit/test_subagent_resume.py::TestBackgroundPoolNotice::test_worker_text_never_appears_in_the_notice` |
| 22 | **B-2:** the delivered background message frames worker text as untrusted subagent output with the pool id, and an instruction-shaped string… *(superseded: superseded by SUBAGENT-11 goal 5)* | `tests/unit/test_subagent_resume.py::TestBackgroundPoolDelivery::test_only_the_runtime_notice_enters_the_user_role` |
| 23 | **D-2:** with no configuration, `SubagentSettings()` arms `max_tokens`, `max_total_time_s`, `max_tool_calls` and `max_iterations` at the values… | `tests/unit/test_config.py::test_subagent_operator_ceilings_are_armed_and_match_the_guide` |
| 24 | **D-2:** for one pool the governor's token count equals the input-plus-output sum of that pool's ledger records. *(Amended in wave 3.)* The… | `tests/unit/test_subagent_pool.py::test_each_worker_reports_its_own_spend_and_the_pool_reports_the_total` |
| 25 | **SUBAGENT-08 criterion 5:** stopping a worker that waits on an approval leaves no answerable prompt for it and leaves a sibling's pending… | `tests/unit/test_subagent_lifecycle.py::test_stopping_a_worker_waiting_on_approval_withdraws_only_its_prompt` |
| 26 | Every SUBAGENT-01…09 document carries an implementation-status section that cites `0a63bfb` (or a later commit) and the WP ids that close its… | The SUBAGENT-01…11 documents' implementation-status sections and `status:` lines |
| 27 | A `spawn_subagents` call refused for its `max_concurrent` value (below 1, or above the ceiling) does not report the refused value as… | `tests/integration/test_subagent_tool_execution.py::test_concurrency_below_one_is_refused_without_reporting_a_used_value`<br>`tests/integration/test_subagent_tool_execution.py::test_accepted_concurrency_is_reported_as_used` |

## SUBAGENT-11

Source: [SUBAGENT-11-model-facing-pool-control.md](SUBAGENT-11-model-facing-pool-control.md)

| # | Criterion (abridged) | Decided by |
|---|---|---|
| 1 | `spawn_subagents(..., background=True)` returns within one second with `status="running"` and a `pool_id` while its workers are still running, and… | `tests/integration/test_subagent_tool_execution.py::test_a_model_requested_background_pool_returns_running_at_once` |
| 2 | `background=True` without a durable journal, or without a supervised task owner, returns `status="background_unavailable"` and starts no worker | `tests/integration/test_subagent_tool_execution.py::test_a_model_requested_background_is_refused_when_it_cannot_be_owned`<br>`tests/integration/test_subagent_tool_execution.py::test_backgrounding_without_a_supervisor_is_refused_and_stops_the_pool` |
| 3 | `background=False` and the Ctrl+B path produce the same aggregate text, event kinds and journal records as at `0a63bfb` | `tests/unit/test_subagent_pool_runtime_coverage.py::test_a_backgrounded_pool_produces_the_same_aggregate_events_and_records` |
| 4 | `collect_subagent_results(pool_id)` on a running pool returns `status="running"` with one row per worker and does not await the pool task | `tests/unit/test_subagent_resume.py::TestCollectSubagentResults::test_a_running_pool_returns_a_snapshot_without_blocking` |
| 5 | `collect_subagent_results(pool_id, wait=True, timeout_s=0.1)` on a pool that outlives the timeout returns the running snapshot with… | `tests/unit/test_subagent_resume.py::TestCollectSubagentResults::test_a_running_pool_returns_a_snapshot_without_blocking`<br>`tests/unit/test_subagent_resume.py::TestCollectSubagentResults::test_a_malformed_wait_bound_is_refused` |
| 6 | Collecting a terminal pool three times returns an identical aggregate each time and never raises `unknown or expired subagent pool` | `tests/unit/test_subagent_resume.py::TestCollectSubagentResults::test_a_finished_pool_is_collected_identically_every_time` |
| 7 | After a simulated restart — fresh registry, no live pool — collecting an `interrupted` pool from the same conversation returns its preserved… | `tests/unit/test_subagent_resume.py::TestCollectSubagentResults::test_after_a_restart_a_pool_is_read_from_the_journal`<br>`tests/unit/test_subagent_resume.py::TestCollectSubagentResults::test_a_pool_from_another_conversation_is_refused` |
| 8 | A background pool that finishes while no turn is running produces exactly one notice at the next turn boundary; the notice contains the pool id,… | `tests/unit/test_subagent_resume.py::TestBackgroundPoolNotice::test_worker_text_never_appears_in_the_notice`<br>`tests/unit/test_tui_session_coverage.py::test_completed_background_subagent_is_delivered_once` |
| 9 | No code path appends worker-authored text to the parent's memory as a user message: after a background completion the parent memory's user-role… | `tests/unit/test_subagent_resume.py::TestBackgroundPoolDelivery::test_only_the_runtime_notice_enters_the_user_role` |
| 10 | With `on_clarification="fail"`, a backgrounded worker's `ask_parent` returns `unanswerable_in_background` in under one second; with `"queue"`, the… | `tests/integration/test_subagent_tool_execution.py::test_on_clarification_fail_ends_a_backgrounded_workers_wait_at_once`<br>`tests/unit/test_subagent_communication.py::test_refusing_parent_questions_ends_waits_and_refuses_new_ones`<br>`tests/unit/test_subagent_communication.py::test_a_backgrounded_workers_question_is_announced_once_without_its_text` |
| 11 | A headless session defaults `on_clarification` to `fail` and an interactive TUI session to `queue` | `tests/unit/test_config.py::test_on_clarification_defaults_follow_the_session_kind` |
| 12 | `stop_subagents(pool_id, worker_id)` — mode omitted, so `abort` — ends that worker with phase `cancelled`, `partial=true` when it had completed a… | `tests/unit/test_subagent_lifecycle.py::test_the_parent_model_stops_one_worker_of_a_running_pool` |
| 13 | `stop_subagents(pool_id, mode="drain")` starts no pending worker and reports the never-started workers, through the same `request_pool_stop` the… | `tests/unit/test_subagent_lifecycle.py::test_the_parent_model_drains_a_pool_and_learns_which_workers_never_start` |
| 14 | `retry_subagent` on a failed worker of a finished pool starts attempt 2 and leaves succeeded workers untouched; on a live worker, a succeeded… | `tests/unit/test_subagent_lifecycle.py::test_a_retry_is_refused_when_the_pool_budget_is_used_up`<br>`tests/unit/test_subagent_lifecycle.py::test_a_retry_is_refused_when_it_would_exceed_the_active_pool_limit`<br>`tests/unit/test_subagent_lifecycle.py::test_retry_refuses_live_and_succeeded_workers_without_changing_lifecycle` |
| 15 | A failed worker of pool A remains retryable through `retry_subagent` after pool B has been spawned and finished in the same conversation, and pool… | `tests/unit/test_subagent_lifecycle.py::test_an_older_pool_stays_retryable_after_a_newer_one_finishes` |
| 16 | `list_subagent_pools()` returns live and journaled pools of this conversation, including `interrupted` ones, and none from another conversation | `tests/unit/test_subagent_communication.py::test_listing_pools_marks_a_background_notice_still_to_deliver`<br>`tests/unit/test_subagent_lifecycle.py::test_an_older_pool_stays_retryable_after_a_newer_one_finishes` |
| 17 | A worker inside a nested pool can stop or retry workers of a pool it spawned and is refused for its sibling's or parent's pool | `tests/integration/test_subagent_tool_execution.py::test_a_nested_worker_retries_its_own_pool_and_is_refused_elsewhere` |
| 18 | A headless session started on a journal with a `running` pool marks it `interrupted` before its first turn and delivers its notice at that turn's… | `tests/unit/test_headless_coverage.py::test_a_headless_session_announces_a_pool_interrupted_by_a_crash` |
| 19 | Session shutdown with a background pool running leaves its journal record terminal (`cancelled`) and its supervised task awaited | `tests/integration/test_subagent_tool_execution.py::test_session_shutdown_cancels_a_backgrounded_pool` |
