#!/usr/bin/env python3
"""
aicommit.py

Generate AI-powered commit messages and PR summaries from git diffs using LiteLLM.

Usage:
  python aicommit.py
  python aicommit.py --staged
  python aicommit.py --apply
  python aicommit.py pr --base origin/main
  python aicommit.py --provider gemini --model gemini/gemini-2.5-flash
  python aicommit.py --provider openai --model openai/gpt-5
  python aicommit.py --provider anthropic --model anthropic/claude-3-7-sonnet-latest

Environment variables:
  GEMINI_API_KEY
  OPENAI_API_KEY
  ANTHROPIC_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AICommitResult:
    title: str
    description: str
    bullets: list[str]
    impact: str
    raw_text: str


SYSTEM_PROMPT = """You are a senior software engineer helping generate high-quality git commit messages and pull request descriptions.

Follow these rules carefully:
- Be precise and grounded only in the provided diff.
- Do not invent changes not present in the diff.
- Prefer imperative mood for commit titles.
- Keep commit titles under 72 characters when possible.
- Return valid JSON only.
- Use this JSON schema exactly:
{
  "title": "string",
  "description": "string",
  "bullets": ["string", "string"],
  "impact": "string"
}
"""

COMMIT_USER_PROMPT = """Analyze the following git diff and generate:
1. A concise commit title
2. A short description paragraph
3. A bullet list of key modifications
4. A short impact statement

Return JSON only.

DIFF:
{diff}
"""

PR_USER_PROMPT = """Analyze the following git diff and generate a pull request style summary with:
1. A concise title
2. A summary paragraph
3. A bullet list of key modifications
4. A short impact statement

Return JSON only.

DIFF:
{diff}
"""


def run_cmd(cmd: list[str], cwd: Optional[str] = None, check: bool = True) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def ensure_git_repo(cwd: Optional[str] = None) -> None:
    try:
        run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "Not inside a git repository. Run this command from a repo root or subdirectory."
        ) from exc


def has_staged_changes(cwd: Optional[str] = None) -> bool:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 1


def has_unstaged_changes(cwd: Optional[str] = None) -> bool:
    result = subprocess.run(
        ["git", "diff", "--quiet"],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 1


def get_diff(mode: str, staged: bool, base: Optional[str], cwd: Optional[str] = None) -> str:
    if mode == "commit":
        if staged:
            cmd = ["git", "diff", "--cached"]
            diff = run_cmd(cmd, cwd=cwd)
            if not diff:
                raise SystemExit("No staged changes found to summarize.")
            return diff

        if has_staged_changes(cwd):
            cmd = ["git", "diff", "--cached"]
            diff = run_cmd(cmd, cwd=cwd)
            if diff:
                return diff

        if has_unstaged_changes(cwd):
            cmd = ["git", "diff"]
            diff = run_cmd(cmd, cwd=cwd)
            if diff:
                return diff

        raise SystemExit("No changes found to summarize.")

    if mode == "pr":
        if not base:
            raise SystemExit("--base is required for pr mode, e.g. --base origin/main")
        cmd = ["git", "diff", f"{base}...HEAD"]
        diff = run_cmd(cmd, cwd=cwd)
        if not diff:
            raise SystemExit(f"No changes found between {base} and HEAD.")
        return diff

    raise SystemExit(f"Unsupported mode: {mode}")


def trim_diff(diff: str, max_chars: int) -> str:
    if len(diff) <= max_chars:
        return diff
    return diff[:max_chars] + "\n\n[...diff truncated for model input size...]"


def get_api_key(provider: str) -> str:
    env_map = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    env_var = env_map[provider]
    api_key = os.getenv(env_var)
    if not api_key:
        raise SystemExit(f"{env_var} is not set.")
    return api_key


def call_litellm(
    *,
    model: str,
    api_key: str,
    provider: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    try:
        from litellm import completion
    except Exception as exc:
        raise SystemExit(
            "LiteLLM is not installed. Install with: pip install litellm"
        ) from exc

    env_var_map = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    os.environ[env_var_map[provider]] = api_key

    response = completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )

    try:
        text = response.choices[0].message.content
    except Exception as exc:
        raise SystemExit("LiteLLM returned an unexpected response shape.") from exc

    if not text:
        raise SystemExit("LiteLLM returned an empty response.")

    return text


def parse_result(raw_text: str) -> AICommitResult:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "Model response was not valid JSON. Try again with a smaller diff or different model.\n"
            f"Raw response:\n{raw_text}"
        ) from exc

    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()
    bullets = data.get("bullets", [])
    impact = str(data.get("impact", "")).strip()

    if not isinstance(bullets, list):
        bullets = []
    bullets = [str(item).strip() for item in bullets if str(item).strip()]

    if not title:
        raise SystemExit("Model output did not include a title.")

    return AICommitResult(
        title=title,
        description=description,
        bullets=bullets,
        impact=impact,
        raw_text=raw_text,
    )


def render_commit_message(result: AICommitResult) -> str:
    lines = [result.title]

    body_parts: list[str] = []
    if result.description:
        body_parts.append(result.description)
    if result.bullets:
        body_parts.extend(f"- {item}" for item in result.bullets)
    if result.impact:
        body_parts.append(f"Impact: {result.impact}")

    if body_parts:
        lines.append("")
        lines.extend(body_parts)

    return "\n".join(lines).strip() + "\n"


def render_pr_summary(result: AICommitResult) -> str:
    parts = [f"# {result.title}"]

    if result.description:
        parts.append("")
        parts.append(result.description)

    if result.bullets:
        parts.append("")
        parts.append("## Changes")
        parts.extend(f"- {item}" for item in result.bullets)

    if result.impact:
        parts.append("")
        parts.append("## Impact")
        parts.append(result.impact)

    return "\n".join(parts).strip() + "\n"


def apply_git_commit(message: str, cwd: Optional[str] = None) -> None:
    subprocess.run(
        ["git", "commit", "-F", "-"],
        cwd=cwd,
        input=message,
        text=True,
        check=True,
    )


def print_output(mode: str, result: AICommitResult) -> str:
    output = render_commit_message(result) if mode == "commit" else render_pr_summary(result)
    print(output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate AI-powered commit messages and PR summaries"
    )

    parser.add_argument(
        "mode",
        nargs="?",
        default="commit",
        choices=["commit", "pr"],
        help="Whether to generate a commit or PR summary (default: commit)",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Force using staged changes in commit mode",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Base branch/ref for PR mode, e.g. origin/main",
    )
    parser.add_argument(
        "--provider",
        choices=["gemini", "openai", "anthropic"],
        default="gemini",
        help="LLM provider used through LiteLLM",
    )
    parser.add_argument(
        "--model",
        default="gemini/gemini-2.5-flash",
        help="Model name for the selected provider",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=50000,
        help="Maximum diff characters sent to the model",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=True,
        help="Apply the generated commit after confirmation",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Optional repository path",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    ensure_git_repo(args.cwd)

    diff = get_diff(
        mode=args.mode,
        staged=args.staged,
        base=args.base,
        cwd=args.cwd,
    )
    diff = trim_diff(diff, args.max_chars)

    prompt = (
        COMMIT_USER_PROMPT if args.mode == "commit" else PR_USER_PROMPT
    ).format(diff=diff)

    api_key = get_api_key(args.provider)

    raw_text = call_litellm(
        model=args.model,
        api_key=api_key,
        provider=args.provider,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=prompt,
    )

    result = parse_result(raw_text)
    output = print_output(args.mode, result)

    if args.apply:
        if args.mode != "commit":
            raise SystemExit("--apply is only supported in commit mode.")

        print("\nProposed commit message:\n")
        print(output)

        confirm = input("Proceed with this commit? [y/N]: ").strip().lower()
        if confirm in {"y", "yes"}:
            apply_git_commit(output, cwd=args.cwd)
            print("Committed successfully.", file=sys.stderr)
        else:
            print("Commit aborted.", file=sys.stderr)


if __name__ == "__main__":
    main()