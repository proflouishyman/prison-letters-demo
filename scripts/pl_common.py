"""Shared helpers for the prison_letters demo pipeline (paths, loud-failure, hashing, timestamps)."""
import datetime
import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "apwa_mini.sqlite"
PDF_DIR = ROOT / "data" / "pdfs"
PAGES_DIR = ROOT / "data" / "pages"
TRANSCRIPTIONS_DIR = ROOT / "transcriptions"
IMAGE_DPI = 200  # contract §2: pages are rendered at 200 dpi


class PipelineError(Exception):
    """Any condition that must stop the run visibly (loud failure)."""


def now_utc():
    """ISO-8601 UTC timestamp, second resolution."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def model_slug(tag):
    """Contract §3: model tag with ':' and '/' replaced by '_'."""
    return tag.replace(":", "_").replace("/", "_")


def git_version():
    """Git short SHA, or the constant 'dev' when the repo has no commit yet (contract §1)."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "dev"
    except OSError:
        return "dev"


def fail(msg, code=1):
    """Print a FAILED banner and exit non-zero."""
    bar = "=" * 30
    print(f"\n{bar} FAILED {bar}\n{msg}\n{bar}{'=' * 8}{bar}", file=sys.stderr)
    sys.exit(code)
