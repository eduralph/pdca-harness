# Test fixtures — provenance

Pinned vendor bytes for suites that must not *synthesise* the shape they then parse.
A rule keyed on a field the CLI never emits is a branch that looks like coverage and is
not — so every claim the production code makes about a vendor event is recorded here as
either **observed** (bytes a real run wrote, pinned verbatim) or **derived** (read out of
the shipped binary's own emitter, with the expression quoted).

Scope of this note: the claims `progress._terminal_error` / `progress._is_subagent_event`
depend on — **the marker** on a terminal API-error report, **whose** report it is, and the
`result` wrap-up that competes with it for precedence. Nothing in this section classifies a
cause; the error *kinds*, typed causes, HTTP statuses and usage-limit record the retry
decision reads are grounded in [Classifying the report](#classifying-the-report-issue-539)
below.

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

## Classifying the report (issue #539)

The claims `progress._reports_transient_cause`, `progress._is_main_session_work` and
`progress._usage_limit_refusal` depend on: which error **kinds** the harness retries and
which of them are the vendor's own rule, where the vendor **types** a cause and which typed
cause is transient, how the text of a report the vendor could **not** classify is built,
where an HTTP **status** reaches the stream, how a **recovered** session shows itself, and
how the stream tells a **spent usage window** from a passing rate-limit rejection. No new
fixture file: the classification cases are built inline in
`test_terminal_error_classification.py` (a C4 red leg reverts any `fixtures/*.jsonl` a
patch adds), and the two pinned death records above are replayed there too.

### Observed

* The incident record (`claude_api_error_death.transcript.jsonl`, 2.1.228) carries
  `"error":"server_error"` on a lost connection, and that leaf exited **1** after it — the
  kind and the exit this classification retries.
* The permanent record (`claude_api_error_permanent.transcript.jsonl`, 2.1.222) carries
  `"error":"model_not_found"` and, in the transcript spelling only, `"apiErrorStatus":404`.
  Its stream spelling (derived above) has no status field — see the next list.
* A usage-limit record, from a 2.1.277 `--output-format stream-json` session on a
  subscription (claude.ai) login, arriving mid-stream after the session's first response:
  `{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1789761600,
  "rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":
  "out_of_credits","isUsingOverage":false,"unifiedWindows":{…}},"uuid":…,"session_id":…}`
  (quoted as `_OBSERVED_ALLOWED` in the test). The normal, **allowed** state names the
  limiting window too, so a `rateLimitType` alone is no refusal.

Neither death record carries a typed cause, so everything below about typed causes is
derived; so is every `rate_limit_event` in the `rejected` state.

### Derived (claude-code 2.1.284, the installed binary; the greps below re-verify)

* **The kind enum** — `z(["authentication_failed","oauth_org_not_allowed",
  "account_on_hold","verification_required","billing_error","rate_limit","overloaded",
  "invalid_request","model_not_found","server_error","unknown","max_output_tokens",
  "cloud_credential_error"])`. It rides the main-loop emitter's `error:n.error`.
* **The vendor's own main-session transient rule** — `function Akr(e){return
  e.apiErrorIsTransient===!0||e.error==="overloaded"||e.error==="server_error"}`. It covers
  `overloaded` and `server_error` only; `rate_limit` is transient there only when the
  `apiErrorIsTransient` flag is set (the generic 429 sets it on a condition:
  `error:"rate_limit",apiErrorIsTransient:M`), and that flag never reaches the stream — the
  wire converter `function _o(e,s){…r={aborted:…,is_api_error_message:…,api_error:…,
  api_error_params:…,api_error_code:…,…}` does not carry it.
* **`rate_limit` is this project's policy, not the vendor's main-session rule** — the
  harness retries a **passing** mid-session rate-limit rejection as the twin of the
  invocation-time one #138 already retried, a choice made at sign-off (#539). A spent
  subscription window is not one, and the usage-limit record below vetoes it. The
  three-kind set is borrowed
  from the CLI's handling of a **sub-agent** an API error cut off: `var rl=new
  Set(["rate_limit","overloaded","server_error"]);function il(e,s){if(!(e instanceof
  Ao))return null;if(!e.errorKind||!rl.has(e.errorKind))return null;…` (`Ao` is
  `AgentApiErrorTerminationError`) — for those kinds it keeps the Task's partial output
  ("Everything below is PARTIAL output recovered from the agent before it was cut off")
  instead of failing it. Hence `_CLAUDE_TRANSIENT_KINDS = {server_error, overloaded,
  rate_limit}`: two kinds from the vendor's main-session rule, one by policy.
* **A lost connection is stamped `server_error`** — ``if(w&&(JF.has(w.code)||J0.has(w.code)))
  return Qo({content:`${Pa}: Connection to the API was lost (${w.code}). This is usually
  temporary — try again.`,error:"server_error"})``, with `JF` = ECONNRESET, EPIPE,
  ConnectionClosed, UND_ERR_SOCKET, ETIMEDOUT, ECONNABORTED, ERR_SOCKET_CLOSED,
  StreamSuspended, StreamTruncated and `J0` = ECONNREFUSED, ConnectionRefused, ENOTFOUND,
  ENETUNREACH, ENETDOWN, EHOSTUNREACH, EHOSTDOWN, EAI_AGAIN, FailedToOpenSocket,
  ERR_PROXY_TUNNEL. A connection that dies mid-stream is stamped the same way by the
  streaming loop (``${Pa}: Connection lost mid-response. The response above may be
  incomplete.`` … `error:"server_error"`), as is a 5xx (`e.status>=500` →
  `error:"server_error"`).
* **Where the vendor types a cause** — the stream field `api_error` (`_o`: `api_error:
  e.apiError`; `apiError` in the transcript spelling, the internal message's own name) is
  the enum `z(["max_output_tokens","dlp_request_denied","claude_code_version_too_old",
  "safety_monitor_blocked","effort_requires_thinking","advisor_incompatible",
  "tool_history_mismatch","autocompact_thrashing","pdf_too_large","pdf_password_protected",
  "no_response","tls_untrusted_ca","gateway_content_type","provider_credentials",
  "gateway_signin_required","gateway_session_expired","api_key_auth_disabled",
  "org_disabled_credential","invalid_credential_header","model_requires_usage_credits",
  "long_context_credits_required","consent_unanswered","no_allowed_fallback",
  "model_substitution_disabled","field_not_granted"])`, described as *"Typed kind of the API
  error … for consumers that key on the cause instead of the message text (the text stays
  the fallback and may change)."* It rides kinds the harness would otherwise retry:
  `error:"rate_limit",apiError:"model_requires_usage_credits"` and
  `error:"rate_limit",apiError:"long_context_credits_required"` (*"a request over the
  200K-token context boundary needs usage credits"*) — usage-credit stops, not rate-limit
  rejections; `error:"server_error",apiError:"no_response"` (*"no response arrived before
  the first-byte deadline on any attempt"*); and `W="tls_untrusted_ca"` /
  `W="gateway_content_type"` → `error:"server_error",apiError:W` (a TLS-inspecting proxy or a
  private CA; a proxy that rewrote the response). The `unknown` kind carries
  `gateway_signin_required` / `gateway_session_expired` / `provider_credentials`. The second
  typed field is `api_error_code` (`apiErrorCode`): *"The server's error.details.error_code
  … Carries server gate codes this build has no api_error value for, so a host can key on a
  new gate without a Claude Code release."* — the wrapper that finishes every API-error
  message sets it (`let S=oWo(g);if(S!==void 0)s.apiErrorCode=S`), and the `success`
  wrap-up repeats it (`…Mt!==void 0&&{api_error_code:Mt}`). Hence
  `_CLAUDE_CAUSE_FIELDS`, and the rule that a typed cause outranks the kind: only
  `no_response` — a response that never came — names a category the harness retries, so
  `_CLAUDE_TRANSIENT_CAUSES = {no_response}`. The schema asks consumers to *"treat an
  unknown value as absent"*; the harness deliberately does not, and treats a typed cause it
  has not read as not transient — that only ever withholds a retry, never grants one.
* **`unknown` is the mapper's fallback, and how its text is built** — ``if(e instanceof
  Pt)return Qo({content:`${Pa}: ${Pre(e)}`,error:"unknown"})`` for an API rejection no
  earlier branch took, with `Pa="API Error"`. `Pt` is the SDK's `APIError`, whose message is
  ``static makeMessage(n,e,t){…if(n&&r)return`${n} ${r}`;…}`` — the status first — and an
  `APIError` with no status is never one: `static generate(n,e,t,r){if(!n||!r)return new
  Fu({message:t,…})`, and `Fu` (`Connection error.`; `Request timed out.` for its timeout
  subclass) is stamped `server_error` earlier. A 5xx and a 429 are stamped earlier too, so
  the status an `unknown` report leads with is a 4xx: 408 and 409 are the ones the API
  client's own retry rule names. The other untyped `unknown` reports are
  ``if(e instanceof Error)return Qo({content:`${Pa}: ${e.message}`,error:"unknown"})`` and a
  bare `return Qo({content:Pa,error:"unknown"})` — a failure that got no HTTP answer at all.
  Hence the two-step read of an `unknown` report's text: a leading `API Error: <status>`
  decides alone (`_UNKNOWN_STATUS_RE`), and without one only the wording of a connection
  that failed counts (`_CONNECTION_FAILED_RE`: the SDK's and the CLI's own phrases above,
  Node's and undici's `socket hang up`, `other side closed`, `terminated`, `fetch failed`,
  `Premature close`, and the `JF`/`J0` codes). Never a bare number — a 400 body names
  message indices such as `messages.536.content.0.text` — and never a bare `timeout`, which
  is a Bash tool input field.
* **Where an HTTP status reaches the stream** — only on the `result` wrap-up's `success`
  variant: `variant:{subtype:"success",api_error_status:Ut,…}` with
  `pt=n.isApiErrorMessage===!0,Ut=n.apiErrorStatus??null,Mt=n.apiErrorCode` taken from the
  last main-loop assistant message, and `is_error:pt`. The `error_*` variants are
  `{subtype:…,errors:ws}` with no status. The SDK schema declares an optional
  `api_error_status` on the assistant message, but no emitter in 2.1.284 sets it (`_o` above
  has no such key). The statuses worth another attempt are the API client's own retry rule:
  `if(e.status===408)return!0;if(e.status===409)return!0;if(e.status===429)return!0;
  if(e.status>=500)return!0;`.
* **What "the session carried on" looks like** — the main-loop emitter `PYt` writes
  `{type:"assistant",…,parent_tool_use_id:null,…,error:n.error,…_o(n),…}` and, for a tool
  result, `{type:"user",message:n.message,session_id:Y(),parent_tool_use_id:null,…}`. A
  sub-agent's `user` events carry the Task's `parent_tool_use_id` (the `agent_progress`
  branch: `case"user":{yield{type:"user",message:g.message,parent_tool_use_id:
  e.parentToolUseID,…}`). So main-session `assistant` **and** `user` events clear a report
  (the CLI recovered); a sub-agent's never do.
* **A spent usage window, told apart from a passing rate-limit rejection** — both reports
  are `error:"rate_limit"`, and what separates them never reaches the stream. For a 429 on
  a subscription login the mapper writes either
  `Qo({content:cve(F,n),error:"rate_limit",quotaLimits:yQ(e)})` — *"You've hit your session
  limit · resets 3pm"* (`function Yh(e,n,r,s){…return`You've hit your ${e}${n}${g}`}`) — or,
  for a 429 that names no window, ``Qo({content:`${Pa}: ${Ce} · ${Ee||Pe}`,error:
  "rate_limit",apiErrorIsTransient:M})`` with `FHn="Server is temporarily limiting requests
  (not your usage limit)"`; `_o` above carries neither `quotaLimits` nor the flag. The
  split is the CLI's own test for a usage-limit 429, `function _sn(e){return
  Boolean(e.headers?.get?.("anthropic-ratelimit-unified-representative-claim")||
  e.headers?.get?.("anthropic-ratelimit-unified-overage-status"))}`, and it is the one 429
  the CLI will not retry for a subscriber: `if(e.status===429)return!ut()||Sde()||qMt(e)`,
  `function qMt(e){return e.status===429&&!_sn(e)&&!e.headers?.get?.(ysn)}`. What DOES reach
  the stream is the account's usage-limit state, `iK=f(()=>u({type:R("rate_limit_event"),
  rate_limit_info:ajr(),uuid:_(),session_id:o()}).describe("Rate limit event emitted when
  rate limit info changes."))`, built by `function Qio(e,s,n={}){…return{type:
  "rate_limit_event",rate_limit_info:r,uuid:to(),session_id:s}}` and emitted by the headless
  session's status-change listener. `ajr` is `status:z(["allowed","allowed_warning",
  "rejected"])`, `rateLimitType:z(["five_hour","seven_day","seven_day_opus",
  "seven_day_sonnet","seven_day_overage_included","overage"]).optional()`,
  `isUsingOverage:H().optional()`, … — *"Rate limit information for claude.ai subscription
  users"*, so an API-key session has none. A final 429 moves that state before the report
  is yielded (`if(Jo instanceof Pt)hQ(Jo,…)`, then `yield{...FN($s,…)}`):
  `extractQuotaStatusFromError(e,…){if(…||e.status!==429)return;…K=ve.own;…K=Ddt(K,e)…
  this.emitStatusChange(K)}` with `function Ddt(e,n){return{...e,status:"rejected",…}}`, the
  window read from the 429's own claim header (`fve`: `...S&&{rateLimitType:S}`). So any
  final 429 leaves `status:"rejected"`, and only the usage-limit 429 names a window there.
  `isUsingOverage` is `r==="rejected"&&(w==="allowed"||w==="allowed_warning")`: the window
  is spent but paid extra usage serves the requests, and the vendor's own UI shows no error
  for it (`function gdt(e,n){if(e.isUsingOverage){…return null}if(e.status==="rejected")
  return{message:d0n(e,n),severity:"error"}…`). Its mock server names a window on every
  overage state too (`setOverageScenarioHeaders(e,n,r){if(this.exceededLimits.length===0)
  this.exceededLimits=[{type:"five_hour",resetsAt:e}];…}`), so the overage-status arm of
  `_sn` needs no rule of its own. Hence `_usage_limit_refusal`: `status == "rejected"`, a
  named `rateLimitType` (any name — a window this harness has not read still withholds the
  retry), and not `isUsingOverage`; the newest record wins, and the record is account-wide,
  so there is no scope to check. One consequence, stated rather than hidden: a 429 the
  server sends with the window claim is a usage-limit refusal to the harness exactly as it
  is to the CLI, which reports it as *"You've hit your … limit"* and does not retry it.

Not observed: a 2.1.284 stream of a real API-error death, any record carrying a typed
cause, or a `rate_limit_event` in the `rejected` state. The minified names (`Akr`, `rl`,
`_o`, `PYt`, `JF`, `J0`, `Pt`, `Fu`, `Pa`, `Qio`, `_sn`, `qMt`, `Ddt`, `gdt`) change between
builds; grep the literals instead:

```sh
B="$(readlink -f "$(command -v claude)")"          # e.g. …/versions/2.1.284
grep -ao 'new Set(\["rate_limit","overloaded","server_error"\])' "$B"
grep -ao 'e.error==="overloaded"||e.error==="server_error"' "$B"
grep -ao 'error:"\(rate_limit\|server_error\)",apiError[:A-Za-z_"]*' "$B" | sort -u
grep -ao '[A-Za-z]="\(tls_untrusted_ca\|gateway_content_type\)"' "$B"
grep -ao 'Carries server gate codes[^.]*' "$B"
grep -ao 'error:"unknown"[^)]\{0,60\}' "$B" | sort -u
grep -ao 'static makeMessage(.\{0,260\}' "$B"
grep -ao 'if(e.status===408).\{0,120\}' "$B"
grep -ao 'return{type:"rate_limit_event",rate_limit_info:[a-z]*' "$B"
grep -ao 'status:z(\["allowed","allowed_warning","rejected"\]),resetsAt' "$B"
grep -ao 'get?.("anthropic-ratelimit-unified-representative-claim")||[^)]*)' "$B" | sort -u
grep -ao 'Server is temporarily limiting requests (not your usage limit)' "$B"
grep -ao '{return{...e,status:"rejected",...[A-Za-z]*(n)}}' "$B"
```

## A vendor sandbox that cannot start (issue #526)

The claims `leaves._BashSandboxProbe` and `leaves._sandbox_refusal` depend on: how a
claude leaf reports that the sandbox the harness seeded (`_seed_plan_sandbox_settings`:
`enabled`, `allowUnsandboxedCommands: false`, `failIfUnavailable: true`) could not start.
All four files were written by claude-code **2.1.277** on a host where
`sysctl kernel.apparmor_restrict_unprivileged_userns` is `1` and `unshare -Ur true` fails
(`write failed /proc/self/uid_map: Operation not permitted`), run as
`claude -p --model haiku --permission-mode acceptEdits --allowedTools Read,Bash,Grep,Glob
--setting-sources project --output-format stream-json --verbose` from a directory holding
the seeded `.claude/settings.json`.

| file | spelling | provenance |
| --- | --- | --- |
| `claude_sandbox_cannot_start.stream.jsonl` | `--output-format stream-json` | **observed** — verbatim lines |
| `claude_bash_ran.stream.jsonl` | `--output-format stream-json` | **observed** — verbatim lines |
| `claude_sandbox_refused.stream.jsonl` | `--output-format stream-json` | **observed** — verbatim |
| `claude_sandbox_refused.stderr.txt` | stderr | **observed** — verbatim, bar one line |

* `claude_sandbox_cannot_start.stream.jsonl` — the defect itself. Asked to run `echo hello`,
  the CLI **exited 0** with nothing on stderr; the Bash `tool_result` is `is_error: true`
  with `Exit code 1\napply-seccomp: write /proc/self/setgroups (nested userns is
  capability-restricted; caller must provide CAP_SYS_ADMIN): Permission denied` — the
  command never started. Kept: the Bash `tool_use`, its `tool_result` and the closing
  `assistant` text (the model's own paraphrase, which the harness does not read). Dropped:
  the `system` lines (`init` lists the account's own connectors), `rate_limit_event`, the
  empty `thinking` block and the `result` wrap-up — none is read by the probe.
* `claude_bash_ran.stream.jsonl` — Bash working, for the "a working sandbox keeps today's
  class" cases: `echo hello` → `content: "hello", is_error: false`, then `false` →
  `content: "Exit code 1", is_error: true`, then the closing text `The sandbox is working
  normally. I reviewed the brief and found no issues.` (asked for verbatim). This host
  cannot start a sandbox, so this one run had `sandbox.enabled: false`; a `tool_result`'s
  shape does not depend on the sandbox, only its content does. Same lines kept, same
  lines dropped.
* `claude_sandbox_refused.*` — `failIfUnavailable` doing its job: the same command with
  `bwrap` hidden from `PATH`. **Exit 1** before any API call; stdout is one `result`
  (`subtype: error_during_execution`, `is_error: true`, `errors: ["Sandbox required but
  unavailable: …"]`), stderr is `Error: sandbox required but unavailable: sandbox is
  enabled but dependencies are missing: bubblewrap (bwrap) not installed · …`. The stderr
  file drops only the capture's final blank line, so the patch passes `git diff --check`.

Derived, not observed: `apply-seccomp:` is the prefix of every error the sandbox's helper
prints (`…: write /proc/self/uid_map`, `…: unshare(CLONE_NEWUSER)`,
`…: prctl(PR_SET_SECCOMP)` and more), and `❌ Sandbox Error: …` then exit 1 is what the
CLI prints when the sandbox fails to initialise at startup. `bwrap:` is bubblewrap's own
message prefix. Re-verify against the installed binary:

```sh
B="$(readlink -f "$(command -v claude)")"
grep -aoE 'apply-seccomp: [a-z]+' "$B" | sort -u   # execvp, fork, mount, prctl, unshare, write, …
grep -ao 'Sandbox required but unavailable\|Sandbox Error: ' "$B" | sort -u
```

As with the section above, the reader degrades to today's behaviour on anything it does not
recognise: a Bash failure without one of these prefixes, or a single Bash call that ran,
leaves the run in the class it had before #526.
