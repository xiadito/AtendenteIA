---
name: commit-message
description: Analyse the current working diff and draft a commit message that matches this repo's conventions, flagging any bugs spotted in the diff along the way. Use when the user asks for a commit message, "crie uma mensagem de commit", "escreva o commit", "commita isso", "what should this commit say", or asks to review changes before committing. Does not commit unless explicitly asked.
---

# Commit message

Read the pending changes, understand *why* they were made, and produce a commit
message the user can paste as-is. Reviewing the diff carefully is the point — a
message written from the file list alone is worthless.

## 1. Gather context

Run these together in one batch (they are independent):

```bash
git status --short
git diff --stat HEAD
git log --oneline -12
```

Then read the **full** diff. Do not skip this step and do not summarise from
`--stat`:

```bash
git diff HEAD          # staged + unstaged, tracked files
```

Scope rules:

- If **anything is staged**, describe only the staged changes (`git diff --cached`)
  and say explicitly which unstaged files you are leaving out.
- If **nothing is staged**, describe all tracked modifications.
- **Untracked files** (`??` in status) are not in any diff. List them to the user
  and ask whether they belong in this commit — never silently ignore them.
- If the diff is very large, read it in chunks per directory rather than
  guessing.

## 2. Infer the repo's convention

`git log` is the style guide — never impose a convention the repo does not use.
Read the last ~12 subjects and match:

- **Prefix style**: Conventional Commits (`feat:`, `fix:`, `chore:`), bare
  imperative, ticket ids, or nothing.
- **Language**: write the message in whatever language the history uses, even if
  the user is chatting in another one. Note it if the history is inconsistent
  and pick the dominant style.
- **Body**: does this repo write bodies at all, or subject-only?

## 3. Read the diff for defects

While reading, actively look for things that are wrong *in the new code*:

- typos in identifiers, routes, endpoint names, env var names, string keys
- copy-paste leftovers, debug prints, commented-out blocks, stray `TODO`
- committed secrets, tokens, `.env` contents, absolute local paths
- obvious logic slips: inverted conditions, off-by-one, unhandled `None`,
  a changed function signature whose callers were not all updated

Verify a suspicion cheaply before reporting it — grep for the other call sites,
check the import exists, confirm the endpoint name elsewhere in the file. Report
only what you actually confirmed, with a clickable `file.py:line` reference and
one line on the concrete consequence. If you find nothing, say nothing; do not
invent findings to look thorough.

Report defects **before** the message, and offer to fix them.

## 4. Write the message

- **Subject**: imperative mood, ≤ 72 chars, no trailing period. States what the
  commit *does*, not what the author did ("fix X", not "fixed X").
- **Body** (wrap at 72): the *why*. What was broken, what the symptom was, why
  this approach. The diff already shows the what — do not narrate it line by
  line.
- Mention secondary/unrelated changes in a short closing paragraph so nothing in
  the diff is silently undocumented.

Present it in a fenced code block so it can be copied cleanly.

## 5. Offer a split when the diff has unrelated concerns

If the diff mixes distinct concerns — a bugfix plus a rename, a feature plus
docs — say so and propose the split as a numbered list, each entry with its own
subject and the files it covers. Recommend the split, but present the single
combined message too and let the user choose. Do not restructure their commits
unasked.

## 6. Stop

Print the message and stop. **Do not run `git commit`, `git add`, or `git push`**
unless the user asks. Close by offering to apply any fixes you flagged and to
make the commit.
