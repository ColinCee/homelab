#!/usr/bin/env bash
# Call GitHub Models API to review a PR diff.
# Reads: /tmp/pr_title.txt, /tmp/pr_body.txt, /tmp/pr_diff_truncated.txt
# Outputs: /tmp/review.txt
#
# Required env: GITHUB_MODELS_TOKEN
# Optional env: MODEL (default: openai/gpt-5.4), REASONING_EFFORT (default: high)
set -euo pipefail

MODEL="${MODEL:-openai/gpt-5.4}"
REASONING_EFFORT="${REASONING_EFFORT:-high}"

SYSTEM_PROMPT=$(cat <<'EOF'
You are a senior engineer reviewing a pull request. Review the diff for:

- **Bugs**: Logic errors, off-by-one, null/undefined issues, race conditions
- **Security**: Injection, secrets in code, unsafe deserialization, path traversal
- **Breaking changes**: API contract changes, config format changes
- **Missing edge cases**: Error handling, empty inputs, boundary conditions

## Rules

- Only comment on things that genuinely matter
- Never comment on style, formatting, naming conventions, or trivial issues
- If the PR looks good, say so briefly — don't invent problems
- Group related issues together rather than commenting line-by-line
- Be specific: quote the problematic code and explain why it's wrong
- Suggest a fix when possible
- Keep the review concise and actionable
- End with which model you are, e.g. 🤖 Reviewed by <your model name>
EOF
)

PR_TITLE=$(cat /tmp/pr_title.txt)
PR_BODY=$(cat /tmp/pr_body.txt)
DIFF=$(cat /tmp/pr_diff_truncated.txt)

USER_CONTENT=$(printf "## PR: %s\n\n%s\n\n## Diff\n\n%s" "$PR_TITLE" "$PR_BODY" "$DIFF")

# Build request JSON safely via jq
jq -n \
  --arg model "$MODEL" \
  --arg effort "$REASONING_EFFORT" \
  --arg system "$SYSTEM_PROMPT" \
  --arg user "$USER_CONTENT" \
  '{
    model: $model,
    reasoning: { effort: $effort },
    messages: [
      { role: "system", content: $system },
      { role: "user", content: $user }
    ]
  }' > /tmp/request.json

echo "Calling ${MODEL} (reasoning: ${REASONING_EFFORT})..."

RESPONSE=$(curl -sS --fail-with-body \
  -X POST "https://models.github.ai/inference/chat/completions" \
  -H "Authorization: Bearer ${GITHUB_MODELS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d @/tmp/request.json)

echo "$RESPONSE" | jq -r '.choices[0].message.content' > /tmp/review.txt

if [ ! -s /tmp/review.txt ] || [ "$(cat /tmp/review.txt)" = "null" ]; then
  echo "::error::Empty response from API"
  echo "$RESPONSE" | jq . >&2
  exit 1
fi

echo "Review generated ($(wc -w < /tmp/review.txt) words)"
