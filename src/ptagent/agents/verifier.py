"""Verification agent: checks generated tests statically and (when possible) by running them."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import Agent


class VerifierAgent(Agent):
    name = "verifier"

    def __init__(self, trace, cfg):
        super().__init__(trace)
        self.cfg = cfg

    # ------------------------------------------------------------------ static
    def static_check(self, modules) -> list:
        """Every generated file must parse and contain at least one test."""
        results = []
        for module in modules:
            source = module.path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source)
                tests = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
                ok, error = bool(tests), (None if tests else "no test functions")
            except SyntaxError as exc:
                ok, error = False, f"syntax error: {exc}"
            results.append({"file": module.path.name, "ok": ok, "error": error})
        self.log("static check", files=len(results), valid=sum(r["ok"] for r in results))
        return results

    # ------------------------------------------------------------------ dynamic
    def dynamic_status(self):
        """Return (can_run, reason)."""
        if self.cfg.run_tests == "never":
            return False, "disabled in configuration (verification.run_tests: never)"
        missing = [m for m in ("pytest", "pyspark", "coverage") if importlib.util.find_spec(m) is None]
        if shutil.which("java") is None:
            missing.append("java")
        if missing:
            return False, "missing: " + ", ".join(missing) + " (install requirements.txt and a JDK to enable test execution)"
        return True, ""

    def run_tests(self, paths, source_roots, extra_paths, work_dir: Path, label: str) -> dict:
        can_run, reason = self.dynamic_status()
        if not can_run:
            self.log("test run skipped", label=label, reason=reason)
            return {"status": "skipped", "reason": reason, "label": label}
        work = Path(work_dir) / f"verify_{label}"
        work.mkdir(parents=True, exist_ok=True)
        junit, cov_data, cov_json = work / "junit.xml", work / ".coverage", work / "coverage.json"
        for stale in (junit, cov_data, cov_json):
            if stale.exists():
                stale.unlink()
        python_paths = [str(p) for p in list(source_roots) + list(extra_paths)]
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(python_paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env["PTAGENT_PYTHONPATH"] = os.pathsep.join(python_paths)
        env["COVERAGE_FILE"] = str(cov_data)
        env.setdefault("PYSPARK_PYTHON", sys.executable)
        cmd = [
            sys.executable, "-m", "coverage", "run", "--branch",
            "--source=" + ",".join(str(r) for r in source_roots),
            "-m", "pytest", *[str(p) for p in paths],
            "-q", "--continue-on-collection-errors", "-p", "no:cacheprovider", f"--junitxml={junit}",
        ]
        try:
            proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=self.cfg.verify_timeout_s)
        except subprocess.TimeoutExpired:
            return {"status": "error", "reason": f"timed out after {self.cfg.verify_timeout_s}s", "label": label}
        if not junit.exists():
            tail = (proc.stdout + proc.stderr)[-800:]
            return {"status": "error", "reason": "pytest produced no report: " + tail, "label": label}
        result = self._parse_junit(junit)
        subprocess.run([sys.executable, "-m", "coverage", "json", "-o", str(cov_json)],
                       cwd=work, env=env, capture_output=True, text=True)
        result.update(self._parse_coverage(cov_json))
        result.update({"status": "ran", "label": label})
        self.log("test run", label=label, passed=result["passed"], failed=result["failed"],
                 coverage=result.get("coverage_percent"))
        return result

    @staticmethod
    def _parse_junit(path: Path) -> dict:
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        total = failed = errors = skipped = 0
        failures = []
        for suite in suites:
            for case in suite.iter("testcase"):
                total += 1
                bad = case.find("failure")
                if bad is None:
                    bad = case.find("error")
                if case.find("skipped") is not None:
                    skipped += 1
                elif bad is not None:
                    if case.find("failure") is not None:
                        failed += 1
                    else:
                        errors += 1
                    message = (bad.get("message") or bad.text or "").strip().splitlines()
                    failures.append({
                        "id": f"{case.get('classname', '')}::{case.get('name', '')}",
                        "file": (case.get("classname", "").split(".")[-1]),
                        "name": case.get("name", ""),
                        "message": (message[0] if message else "")[:300],
                    })
        passed = total - failed - errors - skipped
        return {"total": total, "passed": passed, "failed": failed, "errors": errors,
                "skipped": skipped, "failures": failures}

    @staticmethod
    def _parse_coverage(path: Path) -> dict:
        if not path.exists():
            return {"coverage_percent": None, "uncovered": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        uncovered = {f: info.get("missing_lines", []) for f, info in data.get("files", {}).items()}
        return {"coverage_percent": round(data.get("totals", {}).get("percent_covered", 0.0), 1),
                "uncovered": uncovered}
