"""
TradeLens AI — Phase 3E.5.2.7.4
Isolated Git environment-file and runtime-secret protection tests.

Verifies `.gitignore` coverage inside temporary isolated Git repositories so no
real environment files or secrets are created or inspected in the active workspace.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Iterable, List


REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_GITIGNORE = REPO_ROOT / ".gitignore"


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _create_isolated_git_repo(tmp_root: Path, gitignore_src: Path = REPO_GITIGNORE) -> Path:
    repo = tmp_root / "isolated_repo"
    repo.mkdir(parents=True, exist_ok=True)
    init_res = _run_git(repo, "init", "-q")
    if init_res.returncode != 0:
        raise RuntimeError(f"git init failed: {init_res.stderr}")
    _run_git(repo, "config", "user.email", "audit-test@example.invalid")
    _run_git(repo, "config", "user.name", "Audit Test")
    shutil.copy2(gitignore_src, repo / ".gitignore")
    return repo


def _touch_rel_files(base: Path, rel_paths: Iterable[str]) -> List[Path]:
    created: List[Path] = []
    for rel in rel_paths:
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# isolated synthetic test file\n", encoding="utf-8")
        created.append(target)
    return created


class TestGitignoreEnvironmentProtection(unittest.TestCase):
    def test_01_root_and_variant_env_files_are_ignored(self) -> None:
        env_variants = (
            ".env",
            ".env.local",
            ".env.dev",
            ".env.production",
            ".env.staging",
            ".envrc",
            ".envrc.local",
            "local.env",
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = _create_isolated_git_repo(Path(tmp))
            _touch_rel_files(repo, env_variants)

            for rel in env_variants:
                chk = _run_git(repo, "check-ignore", "-q", rel)
                self.assertEqual(
                    chk.returncode,
                    0,
                    msg=f"Expected '{rel}' to be ignored by .gitignore.",
                )

            status = _run_git(repo, "status", "--porcelain", "--untracked-files=all")
            self.assertEqual(status.returncode, 0)
            untracked = [line[3:] for line in status.stdout.splitlines() if line.startswith("?? ")]
            for rel in env_variants:
                self.assertNotIn(rel, untracked)

    def test_02_nested_environment_files_are_ignored(self) -> None:
        nested_variants = (
            "backend/.env",
            "backend/.env.local",
            "backend/app/.env.dev",
            "deploy/.env.production",
            "frontend/.env.local",
            "backend/runtime/.envrc",
            "docs/subdir/local.env",
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = _create_isolated_git_repo(Path(tmp))
            _touch_rel_files(repo, nested_variants)

            for rel in nested_variants:
                chk = _run_git(repo, "check-ignore", "-q", rel)
                self.assertEqual(
                    chk.returncode,
                    0,
                    msg=f"Expected nested env file '{rel}' to be ignored.",
                )

            dry_add = _run_git(repo, "add", "--dry-run", *nested_variants)
            self.assertNotEqual(
                dry_add.returncode,
                0,
                msg="git add --dry-run must refuse ignored nested environment files.",
            )

    def test_03_local_runtime_secret_and_key_files_are_ignored(self) -> None:
        secret_files = (
            "runtime_state/remote_auth.json",
            "runtime_state/tls/tradelens-home.crt",
            "runtime_state/tls/tradelens-home.key",
            "runtime_logs/engine.log",
            "deploy/launchd/com.tradelens.home-engine.plist",
            "deploy/launchd/com.tradelens.frontend-dashboard.plist",
            "remote_auth.json",
            "tls/cert.pem",
            "tls/key.p12",
            "tls/bundle.pfx",
            "secrets.json",
            "credentials.json",
            "backend/owner.secret",
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = _create_isolated_git_repo(Path(tmp))
            _touch_rel_files(repo, secret_files)

            for rel in secret_files:
                chk = _run_git(repo, "check-ignore", "-q", rel)
                self.assertEqual(
                    chk.returncode,
                    0,
                    msg=f"Expected runtime secret/TLS artifact '{rel}' to be ignored.",
                )

    def test_04_env_example_remains_trackable_at_root_and_nested(self) -> None:
        templates = (
            ".env.example",
            "backend/.env.example",
            "deploy/scripts/.env.example",
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = _create_isolated_git_repo(Path(tmp))
            _touch_rel_files(repo, templates)

            for rel in templates:
                chk = _run_git(repo, "check-ignore", "-q", rel)
                self.assertEqual(
                    chk.returncode,
                    1,
                    msg=f"Expected template '{rel}' NOT to be ignored.",
                )

            add_res = _run_git(repo, "add", *templates)
            self.assertEqual(add_res.returncode, 0, msg=add_res.stderr)

            ls_res = _run_git(repo, "ls-files", "--cached")
            tracked = set(ls_res.stdout.splitlines())
            for rel in templates:
                self.assertIn(rel, tracked)

    def test_05_existing_tracked_files_are_not_ignored_or_removed(self) -> None:
        ignored_tracked = _run_git(REPO_ROOT, "ls-files", "-c", "-i", "--exclude-standard")
        self.assertEqual(ignored_tracked.returncode, 0, msg=ignored_tracked.stderr)
        self.assertEqual(
            ignored_tracked.stdout.strip(),
            "",
            msg=f"Tracked files must not match .gitignore: {ignored_tracked.stdout}",
        )

        # Verify tracked paths remain present and un-ignored (never silently removed)
        all_tracked = _run_git(REPO_ROOT, "ls-files", "-c")
        self.assertEqual(all_tracked.returncode, 0, msg=all_tracked.stderr)
        self.assertGreater(len(all_tracked.stdout.splitlines()), 50)

        important_tracked = (
            ".env.example",
            ".gitignore",
            ".ops_state/.stage_b_progress.lock",
            ".ops_state/stage_b_progress.json",
            "backend/app/data/ledger_store.json",
            "deploy/launchd/README.md",
            "deploy/scripts/run_launchd_service.sh",
            "data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json",
            "data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json",
            "data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json",
            "data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv",
            "mise.toml",
        )
        for rel in important_tracked:
            self.assertTrue((REPO_ROOT / rel).exists(), msg=f"Missing expected file {rel}")
            tracked_res = _run_git(REPO_ROOT, "ls-files", "--error-unmatch", rel)
            self.assertEqual(tracked_res.returncode, 0, msg=f"{rel} is no longer tracked")
            ignored_res = _run_git(REPO_ROOT, "check-ignore", "-q", rel)
            self.assertEqual(ignored_res.returncode, 1, msg=f"{rel} matched .gitignore")

    def test_06_no_real_environment_file_exists_or_created_in_workspace(self) -> None:
        forbidden_names = {".env", ".env.local", ".env.dev", ".env.production", ".envrc"}
        found_real_env: List[str] = []
        for pattern in (".env*", "*.env"):
            for path in REPO_ROOT.rglob(pattern):
                if ".git" in path.parts or ".venv" in path.parts or "node_modules" in path.parts:
                    continue
                if path.is_file() and (
                    path.name in forbidden_names
                    or path.suffix == ".env"
                    or path.name != ".env.example"
                ):
                    found_real_env.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(
            found_real_env,
            [],
            msg=f"Unexpected real/non-template env files in repository: {sorted(set(found_real_env))}",
        )


if __name__ == "__main__":
    unittest.main()
