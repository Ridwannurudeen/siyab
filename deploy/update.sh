#!/bin/sh
# usage: deploy/update.sh <full 40 character commit sha>
# Tests the commit in a temporary worktree first. Only when the suite and the hygiene check are green
# does the live checkout move to it and the services restart, so a red commit is never checked out
# where the services run. Run the live checkout's own copy: the script updates the repository it
# sits in.
set -eu

if [ "$#" -ne 1 ] || ! printf '%s\n' "$1" | grep -Eq '^[0-9a-f]{40}$'; then
    printf 'usage: %s <full 40 character commit sha>\n' "$0" >&2
    exit 2
fi

revision=$1
repo=$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)
cd "$repo"
old=$(git rev-parse HEAD)

printf 'step 1: check that the live checkout %s at %s is clean\n' "$repo" "$old"
if [ -n "$(git status --porcelain)" ]; then
    printf 'update stopped: working tree has changes\n' >&2
    exit 1
fi

printf 'step 2: fetch %s from origin\n' "$revision"
git fetch origin "$revision"

work=$(mktemp -d)
trap 'rm -rf -- "$work"; git worktree prune' EXIT
printf 'step 3: check out %s in the temporary worktree %s\n' "$revision" "$work/tree"
git worktree add --detach "$work/tree" "$revision"

printf 'step 4: run the suite in the temporary worktree\n'
tests_exit=0
(cd "$work/tree" && python3 -m unittest discover -s tests) >"$work/tests.log" 2>&1 || tests_exit=$?
tests_line="exit $tests_exit, $(sed -n -e '/^Ran /p' -e '/^OK/p' -e '/^FAILED/p' "$work/tests.log" | paste -s -d ' ' -)"

printf 'step 5: run the hygiene check in the temporary worktree\n'
hygiene_exit=0
(cd "$work/tree" && python3 scripts/check_hygiene.py --all) >"$work/hygiene.log" 2>&1 || hygiene_exit=$?
hygiene_line="exit $hygiene_exit, $(wc -l <"$work/hygiene.log" | tr -d ' ') lines of output"

restarted=no
if [ "$tests_exit" -eq 0 ] && [ "$hygiene_exit" -eq 0 ]; then
    printf 'step 6: green, switch the live checkout to %s\n' "$revision"
    git checkout --detach "$revision"
    set -- shieldbot-near-feed.service shieldbot-near-api.service \
        siyab-api.service siyab-delivery.service siyab-history.service \
        siyab-investigation.service siyab-monitor.service
    printf 'step 7: restart %s\n' "$*"
    if sudo systemctl restart "$@"; then
        restarted=yes
    else
        restarted='NO (restart failed)'
        printf 'restart failed: restore the previous checkout %s\n' "$old" >&2
        git checkout --detach "$old"
        if sudo systemctl restart "$@"; then
            printf 'previous checkout restarted\n' >&2
        else
            printf 'previous checkout restart also failed\n' >&2
        fi
    fi
else
    printf 'step 6: red, the live checkout stays at %s and no service restarts\n' "$old"
fi

printf 'old sha: %s\nnew sha: %s\ntests: %s\nhygiene: %s\nservices restarted: %s\n' \
    "$old" "$(git rev-parse HEAD)" "$tests_line" "$hygiene_line" "$restarted"
if [ "$restarted" != yes ]; then
    exit 1
fi
