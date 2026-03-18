# Agent Role: Git Release Manager

## Protocol: Feature Delivery Workflow
When the user asks to "ship this," "create a PR," or "save my work," you must follow this strict sequence. Do not skip steps.

### Phase 1: Branching
1. **Check Status:** Run `git status` to see what has changed and `git branch --show-current` to identify the current branch.
2. **Main Branch Protection:** If the current branch is `main`, you **must** create a new branch. Direct commits to `main` are prohibited.
3. **Branch Naming:**
   - specific feature? -> `feature/brief-description`
   - bug fix? -> `fix/brief-description`
   - vague? -> ask the user for a branch name.
4. **Create Branch:** Run `git switch -c <branch_name>`. (If branch exists, use `git switch <branch_name>`).

### Phase 2: Committing
1. **Stage:** Run `git add .` (unless user specifies specific files).
2. **Commit:** Generate a semantic commit message based on the changes (e.g., `feat: add user login`, `fix: resolve nav overlap`).
3. **Execute:** `git commit -m "<message>"`

### Phase 3: Pushing & PR
1. **Push:** Run `git push -u origin <branch_name>`.
2. **Create PR:** Use the GitHub CLI.
   - Run: `gh pr create --fill`
   - *Note:* `--fill` will auto-generate the title and body from your commit messages.
   - If `--fill` fails or is too vague, generate a title/body and run: `gh pr create --title "feat: <title>" --body "<summary>"`

## Protocol: Testing & Quality
1. **Always Consider Tests**: For every new feature or bug fix, evaluate if unit tests (in `WhiskeyTracker.Tests`) are required.
2. **Planning Phase**: Every `implementation_plan.md` **must** include a "Verification Plan" with both "Automated Tests" and "Manual Verification" sections.
3. **Execution Phase**: Implement tests alongside the code. Ensure they follow established patterns.
4. **Verification Phase**: 
   - Run `dotnet test` and report results.
   - Perform manual verification using the `browser_subagent` when UI changes are involved.
   - Summarize all testing in the `walkthrough.md`.

## Error Handling
- If `gh` commands fail due to auth, first check if the `.github_token` file exists in the root of the project. If it does, run the command with `$env:GH_TOKEN=$(cat .github_token);` prepended. If it still fails, stop and ask the user.
- If there are merge conflicts, **stop** and ask the user for guidance.