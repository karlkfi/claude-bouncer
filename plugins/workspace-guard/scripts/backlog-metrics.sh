#!/usr/bin/env bash
#
# backlog-metrics.sh — replay the backlog file's git history into per-item
# events and summary flow metrics.
#
# The backlog process makes this possible without any recording step: every
# mutation is an isolated commit to one file, IDs are stable, and the
# **Next ID:** counter counts cumulative arrivals. This script only reads.
#
# Usage:
#   backlog-metrics.sh [--events] [path/to/STATUS.md]
#
# Default: summary (throughput, cycle time, arrival rate, prune ratio, aging
# WIP). --events: TSV event stream (id, filed date, removed date, days open,
# reason, size, title) for further analysis.
#
# Removal reasons come from the docs(status) commit-subject verbs
# (complete/prune/merge/defer); anything else is counted as "removed" —
# adopt the verb vocabulary to make throughput honest.

set -euo pipefail

MODE=summary
if [[ "${1:-}" == "--events" ]]; then
    MODE=events
    shift
fi

if [[ -n "${1:-}" ]]; then
    FILE="$1"
else
    FILE="$(git rev-parse --show-toplevel)/docs/STATUS.md"
fi

if [[ ! -f "$FILE" ]]; then
    printf 'backlog-metrics: file not found: %s\n' "$FILE" >&2
    exit 2
fi

DIR="$(cd "$(dirname "$FILE")" && pwd)"
BASE="$(basename "$FILE")"

# IDs currently parked in the Deferred table (excluded from aging WIP — they
# were parked by an explicit decision; aging measures rows awaiting one).
DEFERRED_IDS="$(awk -F'|' '
    /^## Deferred/ { d = 1; next } /^## / { d = 0 }
    d && /^\|/ && match($2, /<a id="Q[0-9]+">/) {
        t = substr($2, RSTART, RLENGTH); gsub(/[^0-9]/, "", t); printf "Q%s ", t
    }' "$FILE")"

# Replay oldest-first. Within one commit, an ID present in both -/+ lines is
# an edit (reorder, status flip, table move) — not an add or removal.
git -C "$DIR" log --reverse -p --format='@COMMIT %as %s' -- "$BASE" |
awk -v mode="$MODE" -v today="$(date +%Y-%m-%d)" -v deferred="$DEFERRED_IDS" '
function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }

function days_between(a, b,    cmd, out) {
    # date math via julian-day arithmetic: YYYY-MM-DD -> serial day count
    return jday(b) - jday(a)
}
function jday(d,    y, m, dd, a, yy, mm) {
    y = substr(d,1,4)+0; m = substr(d,6,2)+0; dd = substr(d,9,2)+0
    a = int((14-m)/12); yy = y+4800-a; mm = m+12*a-3
    return dd + int((153*mm+2)/5) + 365*yy + int(yy/4) - int(yy/100) + int(yy/400) - 32045
}

function row_id(line,    t) {
    t = line
    if (match(t, /<a id="Q[0-9]+">/)) {
        t = substr(t, RSTART, RLENGTH); gsub(/[^0-9]/, "", t); return "Q" t
    }
    return ""
}
function row_field(line, n,    parts, c) {
    c = split(substr(line, 2), parts, "|")   # drop diff +/- marker
    return (n <= c) ? trim(parts[n]) : ""
}

function classify(subject) {
    if (subject ~ /[Cc]omplet|[Ff]inish|[Ss]hip|[Dd]one/) return "completed"
    if (subject ~ /[Pp]rune|[Ss]tale|[Dd]rop|[Zz]ombie/)  return "pruned"
    if (subject ~ /[Mm]erge Q|[Dd]edup/)                  return "merged"
    if (subject ~ /[Dd]efer/)                             return "deferred"
    return "removed"
}

function flush_commit(    id, n) {
    for (id in cadd) if (!(id in cdel)) {
        if (!(id in filed)) { filed[id] = cdate; size[id] = csize[id]; title[id] = ctitle[id] }
        n = substr(id, 2) + 0
        if (n > maxid) maxid = n
    }
    for (id in cdel) if (!(id in cadd)) {
        removed[id] = cdate; reason[id] = classify(csubj)
    }
    delete cadd; delete cdel; delete csize; delete ctitle
}

/^@COMMIT / {
    flush_commit()
    cdate = $2
    csubj = ""; for (i = 3; i <= NF; i++) csubj = csubj (i>3?" ":"") $i
    next
}
/^\+\|/ {
    id = row_id($0)
    if (id != "") {
        cadd[id] = 1
        t = row_field($0, 3); gsub(/\]\([^)]*\)/, "", t); gsub(/\[/, "", t)
        ctitle[id] = t
        if (match($0, /\| *[SML] *\|/)) { s = substr($0, RSTART, RLENGTH); gsub(/[^SML]/, "", s); csize[id] = s }
    }
}
/^-\|/ {
    id = row_id($0)
    if (id != "") cdel[id] = 1
}
/^\+\*\*Next ID:\*\* Q[0-9]+$/ { t = $0; gsub(/[^0-9]/, "", t); counter = t+0 }

END {
    flush_commit()
    if (!counter) counter = maxid + 1   # old-format file: derive from history

    if (mode == "events") {
        print "id\tfiled\tremoved\tdays_open\treason\tsize\ttitle"
        for (id in filed) {
            end = (id in removed) ? removed[id] : ""
            dur = days_between(filed[id], (end != "" ? end : today))
            printf "%s\t%s\t%s\t%d\t%s\t%s\t%s\n", id, filed[id], end, dur, \
                (end != "" ? reason[id] : "open"), size[id], title[id]
        }
        exit
    }

    n_filed = 0; n_done = 0; n_pruned = 0; n_open = 0; n_other = 0
    for (id in filed) {
        n_filed++
        if (!(id in removed)) { n_open++; open_ids[id] = 1; continue }
        r = reason[id]
        if (r == "completed") { n_done++; ct[n_done] = days_between(filed[id], removed[id]) }
        else if (r == "pruned") n_pruned++
        else n_other++
    }

    print "backlog metrics — " (counter ? "counter Q" counter ", " : "") n_filed " items ever filed"
    print ""
    printf "  open now:        %d\n", n_open
    printf "  completed:       %d\n", n_done
    printf "  pruned:          %d  (prune ratio %.0f%% of resolved)\n", n_pruned, \
        (n_done+n_pruned+n_other ? 100*n_pruned/(n_done+n_pruned+n_other) : 0)
    if (n_other) printf "  other removals:  %d  (no verb in commit subject — adopt complete/prune/merge/defer)\n", n_other
    if (n_done) {
        for (i = 2; i <= n_done; i++) { v = ct[i]; j = i-1   # insertion sort for the median
            while (j >= 1 && ct[j] > v) { ct[j+1] = ct[j]; j-- } ct[j+1] = v }
        med = (n_done % 2) ? ct[int(n_done/2)+1] : (ct[n_done/2] + ct[n_done/2+1]) / 2
        sum = 0; for (i = 1; i <= n_done; i++) sum += ct[i]
        printf "  cycle time:      median %.0f days, mean %.1f days (filed -> completed)\n", med, sum/n_done
    }
    print ""
    split(deferred, dtmp, " ")
    for (i in dtmp) is_deferred[dtmp[i]] = 1
    n_def = 0
    for (id in open_ids) if (id in is_deferred) n_def++
    if (n_def) printf "  parked in Deferred: %d (excluded from aging WIP)\n", n_def

    if (n_open > n_def) {
        print "  aging WIP (open Queue rows by ID gap — the groom staleness signal):"
        no = 0
        for (id in open_ids) if (!(id in is_deferred)) order[++no] = id
        for (i = 2; i <= no; i++) {         # sort by gap descending (oldest first)
            v = order[i]; j = i - 1
            while (j >= 1 && (substr(order[j],2)+0) > (substr(v,2)+0)) { order[j+1] = order[j]; j-- }
            order[j+1] = v
        }
        for (i = 1; i <= no; i++) {
            id = order[i]
            gap = counter - (substr(id,2)+0)
            printf "    %-6s gap %-4d filed %s  %s %s\n", id, gap, filed[id], size[id], substr(title[id], 1, 60)
        }
    }
}
'
