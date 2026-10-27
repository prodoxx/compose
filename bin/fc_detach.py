"""Which long commands start their work in a session of their own, and what a Ctrl-C ends. Imports only lower modules."""
import os, signal, sys
import fc_core, fc_proc

DETACHED = ("run", "verify", "resume")  # the long commands: a closed tab or a crashed session must not end them

def detaches(cmd, marker, joins_live):
    """Whether `cmd` must start its work in a session of its own: a long command whose parent did not (`marker`, COMPOSE_DETACHED, is set only by `follow`), a nested one included, and that does not just queue on a live run (`joins_live()` is asked last)."""
    return cmd in DETACHED and not marker and not joins_live()

def interrupt_work(group):
    """Ctrl-C in the follower: the SIGINT goes to the work's whole process group, for a person stopping it means the work, not just this view of it."""
    os.killpg(group, signal.SIGINT)

def interrupted(cmd):
    """Ctrl-C in the work itself: stop what it started in groups of their own (RUNNING), then run the hooks, then say so on stderr; the exit code, 130."""
    fc_proc.stop()
    print(f"\n[{fc_core.now()}] {cmd} stopped by Ctrl-C" + ("; `compose resume` continues it" if cmd in ("run", "resume") else ""), file=sys.stderr)
    return 130
