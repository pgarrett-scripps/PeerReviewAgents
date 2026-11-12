#!/usr/bin/env bash
# Launch the 0.9 plugin spike headlessly, the way `peerreview review` will.
#
# Two Claude Code runs, on purpose:
#
#   1. A probe that calls no tool, only so the `system/init` event can be read.
#      The run stops at once unless that event shows the plugin, the peerreview
#      MCP server connected and the three tool names. The probe also tells us
#      the exact tool names to put in the allow list, instead of guessing them.
#   2. The real run: the skill starts the workflow, the workflow runs the
#      reviewer, and the reviewer stores a finding.
#
# Never `--bare`: bare mode skips plugin sync and hooks and reads auth only
# from ANTHROPIC_API_KEY or apiKeyHelper, so it would not use the subscription
# login at all.
#
# Usage: plugins/prspike/spike.sh [manuscript.pdf] [run-folder]

set -euo pipefail

PLUGIN_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd -- "$PLUGIN_DIR/../.." && pwd)
PDF=${1:-$PROJECT_DIR/tests/fixtures/single-column.pdf}
OUT=${2:-$PROJECT_DIR/.spike-run}
PREFIX=mcp__plugin_prspike_peerreview__
WANTED=("${PREFIX}ingest" "${PREFIX}read_blocks" "${PREFIX}submit_finding")

die() { printf 'spike: %s\n' "$1" >&2; exit 1; }

command -v claude >/dev/null || die "claude is not on PATH"
command -v jq >/dev/null || die "jq is not on PATH"
[ -x "$PROJECT_DIR/.venv/bin/python" ] || die ".venv/bin/python is missing: run 'uv sync --extra dev'"
[ -f "$PDF" ] || die "no such manuscript: $PDF"

mkdir -p "$OUT"
PROBE_LOG=$OUT/probe.jsonl
RUN_LOG=$OUT/run.jsonl

echo "spike: probing the session (plugin $PLUGIN_DIR)"
claude -p 'Reply with the single word ready. Call no tool.' \
  --plugin-dir "$PLUGIN_DIR" \
  --permission-mode dontAsk \
  --output-format stream-json --verbose \
  >"$PROBE_LOG" || die "the probe run failed; see $PROBE_LOG"

INIT=$(jq -c 'select(.type == "system" and .subtype == "init")' "$PROBE_LOG" | head -1)
[ -n "$INIT" ] && [ "$INIT" != "null" ] || die "no system/init event in $PROBE_LOG"

printf 'spike: init event\n'
printf '%s\n' "$INIT" | jq '{
  model,
  mcp_servers,
  plugins: (.plugins // "no plugins key"),
  agents: ((.agents // []) | map(select(test("prspike")))),
  peerreview_tools: ((.tools // []) | map(select(startswith("mcp__plugin_prspike_"))))
}'

SERVERS=$(printf '%s\n' "$INIT" | jq -r '[(.mcp_servers // [])[] | select((.name // "") | test("peerreview")) | "\(.name)=\(.status)"] | join(" ")')
[ -n "$SERVERS" ] || die "the init event lists no peerreview MCP server; the plugin did not load"
case "$SERVERS" in
  *=connected*) : ;;
  *) die "the peerreview MCP server did not connect: $SERVERS" ;;
esac

TOOLS=$(printf '%s\n' "$INIT" | jq -r '[(.tools // [])[] | select(startswith("'"$PREFIX"'"))] | join(",")')
for tool in "${WANTED[@]}"; do
  case ",$TOOLS," in
    *",$tool,"*) : ;;
    *) die "the init event does not list $tool; it listed: ${TOOLS:-nothing}" ;;
  esac
done
echo "spike: $SERVERS, three tools present"

echo "spike: running the workflow on $PDF"
claude -p "Use the peer-review-spike skill from the prspike plugin. Run the spike on the manuscript at $PDF and write the run folder to $OUT." \
  --plugin-dir "$PLUGIN_DIR" \
  --permission-mode dontAsk \
  --allowed-tools "Workflow,Skill,TodoWrite,$TOOLS" \
  --output-format stream-json --verbose \
  --forward-subagent-text \
  >"$RUN_LOG" || die "the workflow run failed; see $RUN_LOG"

printf 'spike: result event\n'
jq -c 'select(.type == "result") | {subtype, is_error, num_turns, duration_ms, total_cost_usd, usage, result}' "$RUN_LOG"

# The authoritative check, outside the model: the finding is on disk.
[ -s "$OUT/findings.jsonl" ] || die "no finding was stored in $OUT/findings.jsonl"
printf 'spike: findings stored\n'
jq -c '{severity, claim, blocks, receipt}' "$OUT/findings.jsonl"
echo "spike: ok"
