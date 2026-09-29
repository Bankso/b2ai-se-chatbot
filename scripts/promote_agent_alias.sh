#!/usr/bin/env bash
# Point a copilot stack's Bedrock agent alias at a new version of the agent
# built from its current DRAFT.
#
# Why: the template's AgentAlias has no RoutingConfiguration, and a stack
# update that changes only the agent (its Instruction or action-group
# OpenAPI) leaves the alias resource untouched. AutoPrepare refreshes the
# DRAFT, but the alias keeps serving the version it was created with. This
# was confirmed on the dev stack on 2026-09-29: the alias was still on version
# 1 while the DRAFT had the new tool definitions. Updating the alias without a
# routing configuration makes Bedrock snapshot the DRAFT as a new version and
# route to it.
#
# The alias is only updated when the DRAFT has changed since the version the
# alias routes to, so re-running a deploy doesn't pile up identical versions.
#
# Usage:
#     scripts/promote_agent_alias.sh <stack-name>
#
# Uses the normal AWS CLI credential chain; set AWS_PROFILE / AWS_REGION in
# the environment to pick an identity (the Makefile does this).

set -euo pipefail

stack="${1:?usage: $0 <stack-name>}"

output() {
  aws cloudformation describe-stacks --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

agent_id="$(output AgentId)"
alias_id="$(output AgentAliasId)"
if [ -z "$agent_id" ] || [ "$agent_id" = "None" ] || [ -z "$alias_id" ] || [ "$alias_id" = "None" ]; then
  echo "ERROR: stack $stack has no AgentId/AgentAliasId output" >&2
  exit 1
fi

alias_name="$(aws bedrock-agent get-agent-alias --agent-id "$agent_id" --agent-alias-id "$alias_id" \
  --query 'agentAlias.agentAliasName' --output text)"
routed="$(aws bedrock-agent get-agent-alias --agent-id "$agent_id" --agent-alias-id "$alias_id" \
  --query 'agentAlias.routingConfiguration[0].agentVersion' --output text)"

# Wait for the DRAFT to finish preparing (AutoPrepare runs during the stack update).
for _ in $(seq 60); do
  status="$(aws bedrock-agent get-agent --agent-id "$agent_id" --query 'agent.agentStatus' --output text)"
  [ "$status" = "PREPARED" ] && break
  case "$status" in FAILED|NOT_PREPARED) echo "ERROR: agent $agent_id is $status" >&2; exit 1 ;; esac
  sleep 5
done
[ "$status" = "PREPARED" ] || { echo "ERROR: agent $agent_id still $status after 5 minutes" >&2; exit 1; }

draft_updated="$(aws bedrock-agent get-agent --agent-id "$agent_id" --query 'agent.updatedAt' --output text)"
if [ "$routed" != "DRAFT" ] && [ "$routed" != "None" ]; then
  version_updated="$(aws bedrock-agent get-agent-version --agent-id "$agent_id" --agent-version "$routed" \
    --query 'agentVersion.updatedAt' --output text)"
  # ISO-8601 timestamps in the same format and zone compare correctly as strings.
  if [[ ! "$draft_updated" > "$version_updated" ]]; then
    echo "Alias $alias_name ($alias_id) already routes to version $routed, which is current with the DRAFT."
    exit 0
  fi
fi

echo "Alias $alias_name ($alias_id) routes to version $routed; DRAFT changed at $draft_updated. Creating a new version..."
aws bedrock-agent update-agent-alias --agent-id "$agent_id" --agent-alias-id "$alias_id" \
  --agent-alias-name "$alias_name" --query 'agentAlias.agentAliasStatus' --output text >/dev/null

for _ in $(seq 60); do
  status="$(aws bedrock-agent get-agent-alias --agent-id "$agent_id" --agent-alias-id "$alias_id" \
    --query 'agentAlias.agentAliasStatus' --output text)"
  [ "$status" = "PREPARED" ] && break
  [ "$status" = "FAILED" ] && { echo "ERROR: alias update failed" >&2; exit 1; }
  sleep 5
done
[ "$status" = "PREPARED" ] || { echo "ERROR: alias $alias_id still $status after 5 minutes" >&2; exit 1; }

now_routed="$(aws bedrock-agent get-agent-alias --agent-id "$agent_id" --agent-alias-id "$alias_id" \
  --query 'agentAlias.routingConfiguration[0].agentVersion' --output text)"
echo "Alias $alias_name ($alias_id) now routes to version $now_routed."
