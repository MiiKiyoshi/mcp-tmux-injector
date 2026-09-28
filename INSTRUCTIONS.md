# tmux-injector

Panes are the user's live terminals: the same shell, PATH and environment as the user, and every command has real consequences.

Sessions: do your work in a session you create with create_session, even when another exists, and kill one-off side sessions when done. In a session you did not create, only run commands in panes that are your task target, registered with set_pane. Killing or respawning anything there takes force=True: pass it only when the user asked. Do not run tmux new-session, kill-session, new-window or kill-window, from your own shell or in a pane.

Events: at the start of every new MCP connection, including after a client or server restart, call wait_events() once and start its script exactly once as it says. Do not poll, start a duplicate, or assume an earlier waiter survived. Unread events are kept until the script starts.

Running code: xsh (shell), xpy (Python REPL) and xtcl (TCL) send code as keystrokes, so they work over ssh too. Use the one that matches the pane's prompt. Each returns the output, or after the timeout (3 s unless set) turns the command into a task that reports [done] on the event stream. The command was already sent: never resend it, and read task_output(task_id) on [done]. Leave timeout unset for long work. A command that changes the prompt (python3, ssh, exit, docker exec) never reports [done]: use read_after=N, which returns the screen N seconds later.

Stopping: send_keys(pane, "C-c") stops a command. task_cancel only stops tracking it.
