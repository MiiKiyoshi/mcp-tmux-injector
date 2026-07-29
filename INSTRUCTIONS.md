# tmux-injector instructions

This describes how you use tmux-injector tools. This is how the system works.

Panes are the user's live terminal sessions.
Same shell, same aliases, same PATH, same environment as if the user typed directly.
Not a sandboxed subprocess: commands have real consequences in the user's environment.

## 1. Tool selection

```text
┌─ Need a fresh workspace (REPL, build, benchmark)?
│   └─→ create_session("<purpose>")   ALWAYS a new, dedicated session.
│       NEVER host your work in a session you did not create this
│       conversation. "Only one session exists" is NOT a reason to
│       reuse it: make your own.
│
┌─ Run a command (any duration)
│   └─→ xsh / xpy / xtcl (default mode)
│       Default timeout=3s (capped at 60s).
│       Completes within timeout → output returned.
│       Exceeds timeout → auto-promotes; returned message contains task_id.
│       Known-slow work leaves timeout unset and follows task_wait.
│
├─ Enter or exit an interpreter / remote shell (prompt changes)
│   └─→ xsh / xpy / xtcl with read_after=N
│       This is a prompt-transition capture: it sends code, sleeps N seconds,
│       and returns the pane content at that moment. Completion-tracked work
│       uses default mode. Prompt transitions use read_after because the
│       completion marker belongs to the previous prompt.
│         xsh(pane, "python3", read_after=2)        # enter Python REPL
│         xpy(pane, "exit()", read_after=1)         # exit Python REPL
│         xsh(pane, "ssh server", read_after=2)     # enter remote shell
│
├─ Wait for an already-promoted task to finish
│   └─→ task_wait(task_id)
│       Returns a path to a wrapper script. Start it with the client-specific
│       completion flow in §2. The script blocks, prints one outcome line,
│       and exits. Call task_output(task_id) for the body.
│
├─ Wait for specific output to appear (no task_id)
│   └─→ poll_pane(pane, pattern, only_new=True|False)
│       Returns a path to a wrapper script. Run via subprocess tool. The
│       script prints "[match] <line>" when the pattern matches.
│       For output whose arrival is SLOW or UNKNOWN (builds, long tasks).
│       A prompt appearing within a second or two of your command
│       (password, yes/no, REPL banner) is a prompt transition, not this:
│       use read_after and read the screen in one call.
│       only_new=True (default): match only output that appears AFTER this call
│       only_new=False: also match content already on screen
│
│       only_new rule:
│         After respawn_pane(cmd=) / create_session(cmd=) → only_new=False
│           cmd= starts process before poll_pane runs → output already on screen
│         After read_after / send_text → only_new=True (default)
│           output arrives after poll_pane snapshot is taken
│
├─ Know how much memory a pane is holding right now
│   └─→ mem_pane(pane | panes=[...])
│       Sums the pane's whole process tree (RSS + GPU), not just the
│       foreground process, and names the heaviest processes. A tool that
│       forks helpers under-reports badly if you only look at the fg pid.
│
├─ Get told when a pane's memory crosses a limit
│   └─→ watch_mem(pane, rss_gb=..., gpu_gb=..., poll=30)
│       Returns a path to a wrapper script. Start it with the client-specific
│       completion flow in §2. Silent while under the cap; on the first breach
│       prints "[cap] <pane>: RSS 42.3 GB > cap 40 GB | top: ... | host avail
│       ... GB, swap used ... GB" and exits.
│       Give rss_gb, gpu_gb, or both: whichever kind of blowup matters.
│       Exits with "[gone]" if the tree ends, so silence never has to be
│       read as "still fine".
│
├─ Respond to prompt (password, yes/no)
│   └─→ send_text (plain text, sends Enter by default)
│
├─ Send special keys (C-c, Escape, arrows)
│   └─→ send_keys (no Enter by default)
│
├─ Reset pane (kill process, fresh shell)
│   └─→ respawn_pane (cleans tasks/locks, keeps registration)
│       cmd= to start specific process after reset
│
├─ Check current screen
│   └─→ capture_pane
│
└─ Check sessions, processes
    └─→ ls (compact overview), ls(session=) (detailed tree)
        Memory is not here: use mem_pane, which sums a pane's whole
        process tree instead of annotating a single pid.
```

## 2. Common patterns

### Long-running command completion

An `xsh`, `xpy`, or `xtcl` call that exceeds its inline timeout returns a
`task_id`. Call `task_wait(task_id)` once. It returns a wrapper script path.
Start that script with the client-specific completion flow below. After its
wait completes or its completion notification arrives, call
`task_output(task_id)` for the command body.

#### Claude Code

Pass the wrapper script path to the `command` parameter of the `Monitor`
tool, together with a `description` (required: the call fails without it,
unlike `timeout_ms`/`persistent` which default to 300000ms/false). `Monitor`
returns control immediately and delivers a completion notification when the
wrapper exits.

```text
wait_script = task_wait(task_id="T...")
Monitor(command=wait_script, description="<what you're waiting for>")
# completion notification arrives
task_output(task_id="T...")
```

#### Codex

Run the wrapper in `functions.exec`. `yield_control()` returns control while the
script continues. If `exec_command` yields a session, `write_stdin` keeps
waiting on that same session. `notify()` delivers the completion output without
requiring a later wait call on the exec cell.

```javascript
const waitScript = "/tmp/tmix_w_...sh"; // returned by task_wait
const pending = (async () => {
  let result = await tools.exec_command({
    cmd: waitScript,
    yield_time_ms: 30000,
  });
  while (result.session_id !== undefined) {
    result = await tools.write_stdin({
      session_id: result.session_id,
      chars: "",
      yield_time_ms: 300000,
    });
  }
  return result;
})();

text("task wait started");
yield_control();
const result = await pending;
notify(result.output.trim());
```

#### Other harnesses

Use the current harness's background command facility when it returns control
immediately and sends a completion interrupt. When that facility does not
exist, run the wrapper script with the harness's `bash` tool and wait for it to
return before calling `task_output(task_id)`.

Waiting for a pattern in an already-running task:
    cmd = poll_pane(pane=pane, pattern="Build complete|ERROR")
    # Run cmd via your subprocess tool. Prints "[match] <line>" on hit.
    #
    # WARNING: only_new=True (default) snapshots the pane state at this
    # call. If the pattern already arrived (quick command finished before
    # this poll_pane) it ends up inside the snapshot and is never matched.
    # Only call once the producing task is still running and the pattern
    # is still pending.

After respawn/create with cmd= (process starts immediately):
    respawn_pane(pane, cmd="python3")
    cmd = poll_pane(pane=pane, pattern=">>>", only_new=False)
    # Run cmd via subprocess tool. only_new=False because ">>>" is already
    # on screen: only_new=True would never match (would be in the snapshot).

Entering a remote shell:
    xsh(pane, "mlx2", read_after=2)         # kubectl exec, ssh, docker exec
    xsh(pane, "ssh -p 8022 user@host", read_after=2)  # password prompt lands in this capture
    send_text(pane, "<password>")           # only if a password is prompted
    xsh(pane, "hostname")                   # commands inside remote shell
    xsh(pane, "python3", read_after=2)      # REPL on the remote host
    xpy(pane, "print(1+1)")                 # xpy works there like a local REPL
    xpy(pane, file="local_script.py")       # file content is embedded and sent
    xpy(pane, "exit()", read_after=1)       # leave the remote REPL
    xsh(pane, "exit", read_after=1)         # return to local shell

    Code travels as keystrokes only: no shared filesystem assumed, so
    everything works identically on remote hosts. file= reads the LOCAL
    path and embeds the content into the payload.

Passing variables from REPL to script:
    xpy(pane, "model_path = '/path/to/model'")
    xpy(pane, file="train.py")     # train.py reads model_path via globals().get(...)
    # file= shares the REPL's globals. Long scripts auto-promote.

Setting up parallel workspaces:
    create_session("exp", windows=["a", "b", "c"], cmd="python3")
    # 3 windows, each running python3, all panes auto-registered

    create_session("exp", windows=["a", "b"], cmds=["python3", "bash"])
    # per-window commands: window a→python3, window b→bash

## 3. Pane registration

Panes must be registered before use.

    set_pane("t1:1.0", "description")
    ls()

"Pane not registered" error → ls() first, then re-register if needed.

Registering a pane ≠ owning its session.
set_pane lets you TALK to an existing pane. It does NOT authorize
creating/killing windows in that pane's session. Host-session choice
is a separate decision: see TOOL SELECTION top box.

## 4. Multi-pane operations

All x* tools accept panes= for parallel dispatch.
code= sends the same code to all panes. codes= sends different code per pane (1:1 indexed).

Same code to all:
    xpy(panes=["t1:a.0", "t1:b.0"], code="print('hello')")

Different code per pane:
    xpy(panes=["t1:a.0", "t1:b.0"], codes=["x=1; print(x)", "x=2; print(x)"])

Per-pane outcome (default mode): each pane independently either returns output
or promotes to a task. The aggregated result lists output for finished panes
and task_id for promoted ones: call task_wait per task_id for any that are
still running.

read_after mode also works with panes=: same N-second wait per pane, all
captured concurrently.

code and codes are mutually exclusive. cmd and cmds are mutually exclusive.
codes/cmds length must match target count.

## 5. Code limitations

xpy / xsh: multi-line code is OK: including compound statements
(for/if/while/def/class/with/try). Code is delivered as one payload,
not typed line-by-line, so REPL continuation prompts are never an issue.
xtcl: literal \n in code breaks: keep TCL code single-line, or call multiple times.

xpy bare expressions produce no output:
    xpy(pane, "x") → empty. Use print(x).

file= path is resolved relative to the agent's working directory, not the pane's cwd.
    xpy(pane, file="train.py")  # /home/user/project/train.py if agent cwd is /home/user/project

Don't use import / reload in the REPL: use file= instead.

## 6. `capture_pane`: `tail` and `grep`

tail= defines the visible window. grep= searches within that window.
    capture_pane(pane, tail=30)                    # last 30 lines
    capture_pane(pane, tail=30, grep="ERROR")      # last 30 lines, then grep
    capture_pane(pane, grep="ERROR")               # last 5 lines (default), then grep

grep does NOT expand the capture range. To search deeper scrollback, increase tail:
    capture_pane(pane, tail=4000, grep="FATAL")    # search last 4000 lines

## 7. State tracking

Pane state is tracked in memory. capture_pane is not called before every command.

State changes to remember:
- xsh(pane, "python3", read_after=2) → pane is now Python REPL: use xpy
- xpy(pane, "exit()", read_after=1)  → pane is back in shell: use xsh
- xsh(pane, "openroad", read_after=2) → pane is now in TCL: use xtcl
- xtcl(pane, "exit", read_after=1)   → pane is back in shell: use xsh
- send_keys(pane, "C-c C-c")          → state uncertain: capture_pane to confirm
- respawn_pane(pane)                  → pane is back in shell, tasks cleaned

capture_pane is used when:
- First time accessing a pane in this conversation
- After Ctrl+C interrupt (state uncertain)

For task progress, use task_status / task_output (not capture_pane).

## 8. Timeout and cancellation

All x* tools send code to tmux before waiting for output.
Any interruption: timeout, abort, user reject, cancel: means code was already sent.

Timeout (default 3s, exceeded):
    Tool returns "[task promoted] T... (pane, Ns)".
    The task is registered automatically. Follow §2 Long-running command
    completion.

User cancellation (CancelledError):
    Auto-converts to background task, same as timeout.
    task_list() identifies it, then task_wait(task_id) waits once.

Long EDA / training / build commands:
    Leave timeout unset. The default timeout promotes the command before the
    MCP client request expires. Follow §2 Long-running command completion.

Exit commands change the prompt: default mode can't detect completion.
Use read_after for exit:
    xtcl(pane, "exit", read_after=1)

A promoted task that NEVER completes usually means the command changed
the prompt (python3 / ssh / exit / docker exec): the end marker can
never appear. send_keys(pane, "C-c"), task_cancel, then retry the same
command with read_after.

Pane death during task:
    task_output(task_id) shows the tmux error reason.
    Recovery: ls() to check pane state → if alive, reuse directly.

task_cancel removes tracking only: execution in tmux continues.
To actually stop: send_keys(pane, "C-c") first, then task_cancel.

## 9. Conversation continuity

Registered panes persist in MCP server memory across conversation compaction.
After compaction: ls() to check what is registered.
New session or server restart: ask user which pane to use.


## 10. Ownership model

managed  = created this session via create_session/create_window.
external = set_pane()-registered. Origin opaque: user, a script, a prior
           agent, anyone. "external" labels the registration path, not ownership.

Agent direct shell: no tmux new-session/kill-session/new-window/kill-window.
Lifecycle via MCP tools. (Scripts invoking tmux internally are unaffected;
their sessions surface as external on first set_pane().)

STRUCTURE IS OFF-LIMITS ON SESSIONS YOU DIDN'T CREATE.
create_window / kill_window are allowed ONLY in sessions you created
via create_session this conversation (managed). For any external or
untracked session, you may register + run commands in its EXISTING
panes (if it's your task target), but NEVER add or remove windows.

"attached" in ls = the user is actively in this session right now.
Hardest read/run-only target: touch nothing structural.

Reading an external label:
  - Name referenced in project workflow or current task → work target, proceed normally.
  - No reference anywhere → treat as user terminal, do not restructure.
  - Ambiguous → ask.

Side work (one-off ssh, sync, file transfer):
    create_session("tmp") → work → kill_session("tmp")
