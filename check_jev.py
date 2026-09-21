"""Check direct TypeSafe access with one small batch of questions."""

import os
from pathlib import Path
import re
import sys
import tomllib

from dotenv import load_dotenv
from typesafe_sdk import Noul, RetryPolicy, TypeSafeClient, TypeSafeError


def main() -> int:
    project_dir = Path(__file__).resolve().parent
    load_dotenv(project_dir / ".env", override=False, interpolate=False)
    config = tomllib.loads((project_dir / "pyproject.toml").read_text(encoding="utf-8"))
    model = config["tool"]["ctrlloop"]["jev-model"]
    if not re.fullmatch(r"jev-\d+\.\d+\.\d+", model):
        print("pyproject.toml must pin an exact Jev version, such as jev-1.13.0.", file=sys.stderr)
        return 1

    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        print(
            "TYPESAFE_API_KEY is missing. Add it to the local .env file "
            "or set it in your process environment.",
            file=sys.stderr,
        )
        return 1

    questions = {
        "asks_for_help": Noul(instructions="Is the writer asking for help?"),
        "expresses_urgency": Noul(instructions="Does the writer express urgency?"),
    }
    try:
        with TypeSafeClient(
            api_key=api_key,
            base_url="https://api.typesafe.ai",
            model=model,
            timeout=30.0,
            retry=RetryPolicy(max_retries=0),
        ) as client:
            response = client.system_one(
                state="Please help me sign in. It is urgent.",
                questions=questions,
            )
    except TypeSafeError as error:
        # Report only the error class/status, never credentials or raw HTTP content.
        status = getattr(error, "status", None)
        print(f"TypeSafe check failed: {type(error).__name__} (HTTP {status}).", file=sys.stderr)
        return 1

    print(f"Requested model: {model}; response model: {response.model}")
    if response.model != model:
        print("Check failed: the response model does not match the pin.", file=sys.stderr)
        return 1
    if set(response.answers) != set(questions) or set(response.nouls) != set(questions):
        print("Check failed: expected one Noul answer per question.", file=sys.stderr)
        return 1
    for name, answer in response.nouls.items():
        if not 0 <= answer.noul <= 1:
            print("Check failed: probability outside [0, 1].", file=sys.stderr)
            return 1
        print(f"{name}: {answer.noul:.4f}")
    print(f"Tokens: {response.usage.input_tokens} input, {response.usage.output_tokens} output")
    print("Connection check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
