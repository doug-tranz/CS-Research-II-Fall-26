```python
import argparse
import json
import os
from pathlib import Path

import anthropic
from google import genai


INPUT_FILE = Path("registration_format_cases.jsonl")
OUTPUT_DIR = Path("model_results")

TASK_INSTRUCTIONS = """
You are evaluating a university course-registration assistant.

Read the supplied registration data and the student's request.
Recommend appropriate sections based on the available information.
Consider course IDs, availability, cancellations, meeting conflicts,
and the student's stated preferences when relevant.

Do not invent section IDs or facts not present in the input.
Explain your recommendation and identify the CRNs you recommend.
Return your answer as plain text or JSON.

Only the registration input is provided below.
"""


def load_cases(limit=None, case_type=None):
    cases = []

    with INPUT_FILE.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue

            try:
                case = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSON on line {line_number}: {error}"
                ) from error

            if case_type and case.get("case_type") != case_type:
                continue

            if "case_id" not in case or "input" not in case:
                raise ValueError(
                    f"Line {line_number} needs case_id and input fields."
                )

            cases.append(case)

            if limit and len(cases) >= limit:
                break

    return cases


def ask_claude(client, model, case):
    prompt = (
        TASK_INSTRUCTIONS
        + "\n\nRegistration input:\n"
        + json.dumps(case["input"], ensure_ascii=False)
    )

    response = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )

    return "\n".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    )


def ask_gemini(client, model, case):
    prompt = (
        TASK_INSTRUCTIONS
        + "\n\nRegistration input:\n"
        + json.dumps(case["input"], ensure_ascii=False)
    )

    response = client.models.generate_content(
        model=model,
        contents=prompt,
    )

    return response.text or ""


def run_provider(provider, cases, model):
    if provider == "claude":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        client = anthropic.Anthropic(api_key=api_key)
        ask = ask_claude

    else:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        client = genai.Client(api_key=api_key)
        ask = ask_gemini

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_file = OUTPUT_DIR / f"{provider}_results.jsonl"

    with output_file.open("w", encoding="utf-8") as file:
        for index, case in enumerate(cases, start=1):
            print(
                f"{provider}: {index}/{len(cases)} "
                f"- {case['case_id']}"
            )

            result = {
                "case_id": case["case_id"],
                "provider": provider,
                "model": model,
            }

            try:
                result["response"] = ask(client, model, case)
                result["status"] = "success"
            except Exception as error:
                result["status"] = "error"
                result["error"] = str(error)
                print(f"  Error: {error}")

            file.write(json.dumps(result, ensure_ascii=False) + "\n")
            file.flush()

    print(f"Saved results to: {output_file}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        choices=["claude", "gemini", "both"],
        default="both",
    )
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument(
        "--case-type",
        choices=["semantic", "validation"],
        default="semantic",
    )
    parser.add_argument(
        "--claude-model",
        default="claude-sonnet-4-5",
    )
    parser.add_argument(
        "--gemini-model",
        default="gemini-2.5-flash",
    )
    args = parser.parse_args()

    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {INPUT_FILE.resolve()}"
        )

    cases = load_cases(
        limit=args.limit,
        case_type=args.case_type,
    )

    if not cases:
        raise ValueError("No matching test cases were found.")

    print(f"Loaded {len(cases)} test case(s).")

    providers = (
        ["claude", "gemini"]
        if args.provider == "both"
        else [args.provider]
    )

    for provider in providers:
        model = (
            args.claude_model
            if provider == "claude"
            else args.gemini_model
        )
        run_provider(provider, cases, model)


if __name__ == "__main__":
    main()
```