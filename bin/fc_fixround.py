"""The pure decisions of a verify fix round: whether the checkout moved under it, and which files a round reverts or may commit.
Imports only lower modules."""
import fc_worktree

def checkout_moved(stood_on, now):
    """Whether the branch the stages ran on is not the one verify started on: a fix commit stays on its branch, only a checkout moves it ("" is a detached HEAD)."""
    return now != stood_on

def void_message(repo, stood_on, now):
    """The line verify ends with when the checkout moved during a stage: nothing verified, nothing recorded."""
    return (f"verify VOID  nothing verified or recorded: {repo} stood on {stood_on or 'a detached HEAD'} when the stages started and stands on "
            f"{now or 'a detached HEAD'} now, so something else checked a branch out there during the run (compose stack, top, a hand checkout) "
            "and the stages judged that code, not the stack top; run verify again")

def unwired(files):
    """The sorted files without blanks and without the agent wiring (fc_worktree.WIRING): trail's and graft's hooks rewrite it, so a fix never commits or reverts it."""
    return sorted(set(files) - {"", *fc_worktree.WIRING})

def revert_plan(fixed, added):
    """(files to check out from HEAD, files to delete) when a fix round is rejected: what the fix changed and HEAD has, and what it added."""
    return sorted(set(fixed) - set(added)), sorted(set(added) & set(fixed))
