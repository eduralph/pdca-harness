# Test fixtures — provenance

Pinned vendor bytes for suites that must not *synthesise* the shape they then parse.
A rule keyed on a field the CLI never emits is a branch that looks like coverage and is
not — so every claim the production code makes about a vendor event is recorded here as
either **observed** (bytes a real run wrote, pinned verbatim) or **derived** (read out of
the shipped binary's own emitter, with the expression quoted).

Scope of this note: the claims `progress._terminal_error` / `progress._is_subagent_event`
depend on — **the marker** on a terminal API-error report, **whose** report it is, and the
`result` wrap-up that competes with it for precedence. Nothing here classifies a cause; a
later change that reads error *kinds* or HTTP statuses appends its own provenance below.

## Files

| file | spelling | provenance |
| --- | --- | --- |
| `claude_api_error_death.transcript.jsonl` | persisted transcript | **observed** — verbatim |
| `claude_api_error_death.stream.jsonl` | `--output-format stream-json` | **derived** from the record above |
| `claude_api_error_permanent.transcript.jsonl` | persisted transcript | **observed** — verbatim |
| `claude_api_error_permanent.stream.jsonl` | `--output-format stream-json` | **derived** from the record above |

### Observed

Both transcript records are pinned **verbatim** from a session log under
`~/.claude/projects/`, one line each, nothing edited:

* `claude_api_error_death.transcript.jsonl` — the incident this retention exists for:
  `"error":"server_error"`, `"isApiErrorMessage":true`, and the text
  `API Error: Connection lost mid-response. The response above may be incomplete.`
  (written by claude-code 2.1.228; `~/.claude/projects/-home-eddie-wyrd-wyrd-pdca/`,
  session `31fa2f21…`, 2026-08-12). The leaf that emitted it filed `(no output captured)`.
* `claude_api_error_permanent.transcript.jsonl` — the same mark with a cause no retry can
  clear: `"error":"model_not_found"`, `"apiErrorStatus":404`, text `There's an issue with
  the selected model …` (written by claude-code 2.1.222;
  `~/.claude/projects/-home-eddie-pdca-pdca-pdca/`, session `152ac920…`, 2026-08-06).
  It is pinned because retention is **unconditional**: a permanent failure must explain
  itself in the bundle exactly as loudly as a transient one.

Also observed, and why the sub-agent scope is spelled two ways: sub-agent records in the
persisted transcript carry `"isSidechain":true` (2080 such records across the corpus these
two were pinned from, e.g. `…/<session>/subagents/agent-*.jsonl`) and carry **no**
`parent_tool_use_id` key at all — which is why `_is_subagent_event` tests `isSidechain is
True` separately instead of reading a missing `parent_tool_use_id` as "the main session".
No marked API-error record with `isSidechain:true` appears in that corpus, so the
*combination* is derived, not observed.

### Derived (claude-code 2.1.234, the installed binary; grep the expressions to re-verify)

* **The mark, stream spelling** — the main loop's own `assistant` emitter:
  `…session_id:qt(),parent_tool_use_id:null,uuid:r.uuid,timestamp:r.timestamp,error:r.error,
  …r.requestId!==void 0&&{request_id:r.requestId},…r.isApiErrorMessage===!0&&{is_api_error_message:!0}…`
  → on the stream the flag is `is_api_error_message`, the main session's own record
  hard-codes `parent_tool_use_id: null`, and the vendor's kind rides `error`.
* **The mark, transcript spelling** — the same message written back the other way:
  `…t.is_api_error_message===!0&&{isApiErrorMessage:!0}…`, and the schema's own words:
  `is_api_error_message … "@internal True when this assistant message wraps an API error
  (from internal AssistantMessage.isApiErrorMessage)."` → the two spellings are one field,
  which is why `_terminal_error` accepts both.
* **Whose report it is** — the `agent_progress` branch forwards the same mark for a
  sub-agent: `if(e.data.type==="agent_progress"||e.data.type==="skill_progress"){…case
  "assistant": … yield{type:"assistant",message:…,parent_tool_use_id:e.parentToolUseID,
  session_id:qt(),…,error:i.error,…,…i.isApiErrorMessage===!0&&{is_api_error_message:!0}…}`
  → a marked report may be a Task's, and the only thing that says so is the Task's
  `parent_tool_use_id`. Hence `_SUBAGENT_NOTE`: kept as evidence, never as the leaf's death.
* **The `result` wrap-up** — built out of the same run:
  `variant:{subtype:"success",api_error_status:mt,result:rt?Ut:We,…}` with
  `common:{…,is_error:rt,num_turns:ze}`, or `variant:{subtype:"error_during_execution",
  errors:Ur}` → a `result` with `is_error` names the *effect*; the marked assistant report
  before it names the cause. That is the whole reason for `_TERMINAL_PRECEDENCE`.

### Re-verifying

```sh
B="$(readlink -f "$(command -v claude)")"          # e.g. …/versions/2.1.234
grep -ao '.\{200\}is_api_error_message.\{240\}' "$B"
grep -ao 'e.data.type==="agent_progress".\{0,1200\}' "$B"
grep -ao '.\{160\}api_error_status.\{0,200\}' "$B"
```

The vendor auto-updates and this harness pins nothing, so a later CLI may add shapes. The
production reader is written to **degrade to today's behaviour** on anything it does not
recognise — an unmarked message, another family's stream, an unparseable line — rather
than guess; re-running the greps above is how a maintainer checks whether the two shapes
it does know are still emitted.
