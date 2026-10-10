#!/usr/bin/env bash
# Runs ON the instance, uploaded and launched by tools/05-run-benchmark.sh.
#
#   run-bench.sh                 # full suite
#   run-bench.sh B1 B3           # a subset, in order
#
# Exists as a file rather than a command string because building one over
# ssh does not survive contact with quoting. The previous version passed
#   bash -lc 'echo '### B8 ###'; bash scripts/run-single.sh B8; '
# where the benchmark banner's own single quotes closed the outer quoting
# early; the next word then began with '#', so bash treated the rest of the
# line as a comment and silently discarded the redirect, the '&' and the
# confirmation echo. Nothing ran, and no log was created to say so.
#
# Three files are written next to the repo so the driver never has to guess
# what happened:
#   tools-run.pid   this script's PID, written FIRST
#   tools-run.log   all output
#   tools-run.rc    the exit status, written last

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

PIDFILE="$(pwd)/tools-run.pid"
LOGFILE="$(pwd)/tools-run.log"
RCFILE="$(pwd)/tools-run.rc"

rm -f "${RCFILE}"
echo $$ > "${PIDFILE}"          # first, so the driver can stop polling
exec > "${LOGFILE}" 2>&1        # everything from here lands in the log

echo "### run-bench.sh starting $(date -Is)"
echo "### pid=$$  args=${*:-<full suite>}"
echo "### strategy=${CUBE_STRATEGY:-<from .env>} transport=${CUBE_TRANSPORT:-<from .env>}"
echo

rc=0
if (( $# == 0 )); then
  echo "### full suite"
  bash scripts/run-suite.sh || rc=$?
else
  for bid in "$@"; do
    echo
    echo "############### ${bid} ###############"
    # run-single.sh takes exactly one benchmark, so a subset is a loop. The
    # first failure stops the run: later benchmarks would measure a platform
    # already in an unknown state.
    bash scripts/run-single.sh "${bid}" || { rc=$?; echo "### ${bid} FAILED rc=${rc}"; break; }
  done
fi

echo
echo "### run-bench.sh done rc=${rc} $(date -Is)"
echo "${rc}" > "${RCFILE}"
rm -f "${PIDFILE}"
exit "${rc}"
