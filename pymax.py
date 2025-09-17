#!/usr/bin/env python3
"""
pymax - single-file prototype

Usage:
    pymax.py [paths...] [--fix] [--no-fix] [--secure] [--strict] [--report json]

This prototype:
 - Formats (via black if available, else does basic normalization)
 - Lints (mixed tabs/spaces, inconsistent indentation, trailing whitespace)
 - Performs identifier spell-check and suggests fixes (can auto-apply if libcst available and --fix)
 - Scans requirements.txt / pyproject.toml for dependencies
 - Optionally runs pip-audit if available and --secure is passed

Dependencies (recommended):
  pip install black libcst pip-audit packaging

"""
from __future__ import annotations
import argparse
import ast
import os
import sys
import re
import json
from collections import Counter, defaultdict
from difflib import get_close_matches
from typing import List, Tuple, Dict, Set

# Optional libs
try:
    import libcst as cst
    from libcst.metadata import PositionProvider, MetadataWrapper
    HAVE_LIBCST = True
except Exception:
    HAVE_LIBCST = False

try:
    import black
    HAVE_BLACK = True
except Exception:
    HAVE_BLACK = False

try:
    # pip-audit is optional for security checks
    import pip_audit
    HAVE_PIP_AUDIT = True
except Exception:
    HAVE_PIP_AUDIT = False

# --- Utilities ---------------------------------------------------------------

PY_EXT = ".py"

def find_python_files(paths: List[str]) -> List[str]:
    files = []
    for p in paths or ["."]:
        if os.path.isfile(p) and p.endswith(PY_EXT):
            files.append(os.path.abspath(p))
        elif os.path.isdir(p):
            for root, _, fnames in os.walk(p):
                for f in fnames:
                    if f.endswith(PY_EXT):
                        files.append(os.path.join(root, f))
    return sorted(set(files))

def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def write_file(path: str, text: str):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

# --- Simple formatting fallback ---------------------------------------------

def normalize_whitespace(text: str, indent_size: int = 4) -> Tuple[str, List[str]]:
    """
    Basic formatting fallback if black isn't available:
     - converts mixed tabs/spaces to spaces
     - normalizes indentation to indent_size where possible
     - trims trailing whitespace
    Returns (new_text, list_of_messages)
    """
    msgs = []
    lines = text.splitlines()
    new_lines = []
    seen_indent_patterns = set()
    for i, ln in enumerate(lines, 1):
        # strip trailing spaces
        stripped = ln.rstrip("\r\n")
        if stripped != ln:
            msgs.append(f"Line {i}: trailing whitespace removed")
        ln = stripped

        # detect tabs vs spaces
        m = re.match(r"^([ \t]+)", ln)
        if m:
            indent = m.group(1)
            seen_indent_patterns.add(indent)
            if "\t" in indent:
                # convert tabs -> spaces (one tab -> 4 spaces)
                ln = re.sub(r"^\t+", lambda mo: " " * (4 * len(mo.group(0))), ln)
                msgs.append(f"Line {i}: converted leading tabs to spaces")
        new_lines.append(ln)
    # try to guess indent size: check common multiples
    # (We keep simple: convert tabs -> 4 spaces, do not reflow code)
    return ("\n".join(new_lines) + ("\n" if text.endswith("\n") else ""), msgs)

# --- AST utilities for identifier collection -------------------------------

class IdentifierCollector(ast.NodeVisitor):
    def __init__(self):
        self.names: Counter[str] = Counter()
        self.scoped_names: Dict[str, List[str]] = defaultdict(list)
        self.imports: Set[str] = set()
        self.assigned: Counter[str] = Counter()
        self.attr_names: Counter[str] = Counter()
        self.func_defs: Counter[str] = Counter()

    def visit_Name(self, node: ast.Name):
        self.names[node.id] += 1
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        # attribute.x -> collect 'x' as attribute name
        if isinstance(node.attr, ast.expr) and hasattr(node.attr, "id"):
            # weird case; ignore
            pass
        elif isinstance(node.attr, ast.AST):
            # attribute attr
            try:
                attr_name = node.attr.attr
            except Exception:
                attr_name = None
            if attr_name:
                self.attr_names[attr_name] += 1
        else:
            # last resort: fallback str()
            try:
                self.attr_names[getattr(node.attr, "id", str(node.attr))] += 1
            except Exception:
                pass
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                self.assigned[t.id] += 1
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        target = node.target
        if isinstance(target, ast.Name):
            self.assigned[target.id] += 1
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        for n in node.names:
            self.imports.add(n.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            self.imports.add(node.module.split(".")[0])
        for n in node.names:
            if n.name != "*":
                self.imports.add(n.name.split(".")[0])
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.func_defs[node.name] += 1
        self.generic_visit(node)

# --- Identifier typo detection ----------------------------------------------

def detect_identifier_typos(collector: IdentifierCollector, threshold=0.85) -> Dict[str, str]:
    """
    Heuristic: If two identifiers are very similar and one is significantly more frequent,
    offer to rename the less-frequent one to the more-frequent one.
    """
    all_names = list(collector.names.keys()) + list(collector.attr_names.keys())
    all_counts = {**collector.names, **collector.attr_names}
    candidates = {}
    uniq = sorted(set(all_names))
    for i, name in enumerate(uniq):
        # find close matches with difflib
        matches = get_close_matches(name, uniq, n=5, cutoff=threshold)
        for m in matches:
            if m == name:
                continue
            # prefer renaming the less-frequent to the more frequent
            if all_counts.get(m, 0) > all_counts.get(name, 0) * 1.5:
                # don't propose if one is builtin or single-char
                if len(name) <= 1 or len(m) <= 1:
                    continue
                # avoid renaming common words like "id" "op"
                if name.islower() and m.islower():
                    candidates[name] = m
    return candidates

# --- Safe rename via libcst (if available) ---------------------------------

def apply_renames_with_libcst(source: str, renames: Dict[str, str]) -> str:
    """
    Use libcst to do a conservative rename of identifiers in the module.
    This attempts to rename Names and Attribute.attr where applicable.
    """
    if not HAVE_LIBCST:
        raise RuntimeError("libcst not available")

    class Renamer(cst.CSTTransformer):
        def __init__(self, mapping):
            self.mapping = mapping

        def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.CSTNode:
            if original_node.value in self.mapping:
                return updated_node.with_changes(value=self.mapping[original_node.value])
            return updated_node

        def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.CSTNode:
            # attribute.attr: rename attr if matches
            if isinstance(original_node.attr, cst.Name):
                nm = original_node.attr.value
                if nm in self.mapping:
                    return updated_node.with_changes(attr=updated_node.attr.with_changes(value=self.mapping[nm]))
            return updated_node

        def leave_Param(self, original_node: cst.Param, updated_node: cst.Param) -> cst.CSTNode:
            if original_node.name.value in self.mapping:
                return updated_node.with_changes(name=updated_node.name.with_changes(value=self.mapping[original_node.name.value]))
            return updated_node

    module = cst.parse_module(source)
    transformer = Renamer(renames)
    new_mod = module.visit(transformer)
    return new_mod.code

# --- Unused import detection (best-effort) ----------------------------------

def detect_unused_imports(tree: ast.AST) -> List[str]:
    """
    Best-effort: find import names, then see if they appear in the AST as Name nodes.
    This will produce false positives/negatives in complicated cases (imports used via alias, getattr, exec, etc).
    """
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imports.append(n.asname or n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for n in node.names:
                if n.name == "*":
                    # skip star imports
                    continue
                imports.append(n.asname or n.name)
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
    unused = [imp for imp in imports if imp not in used]
    return unused

# --- Dependencies parsing ---------------------------------------------------

def parse_requirements_txt(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    deps = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            deps.append(line)
    return deps

def parse_pyproject_toml_for_deps(path: str) -> List[str]:
    # Simple non-toml-parser fallback: look for [tool.poetry.dependencies] or [project] requires
    if not os.path.exists(path):
        return []
    deps = []
    try:
        import tomllib  # py3.11+
        with open(path, "rb") as f:
            doc = tomllib.load(f)
        # poetry
        deps_section = doc.get("tool", {}).get("poetry", {}).get("dependencies", {})
        if isinstance(deps_section, dict):
            for k, v in deps_section.items():
                if k == "python":
                    continue
                deps.append(f"{k}{'' if v is None else str(v)}")
        # PEP621 [project]
        proj = doc.get("project", {}).get("dependencies", [])
        deps.extend(proj if isinstance(proj, list) else [])
    except Exception:
        # fallback: crude grep
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"\[tool\.poetry\.dependencies\]([\s\S]*?)\n\[", text)
        if m:
            chunk = m.group(1)
            for ln in chunk.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                k = ln.split("=")[0].strip()
                deps.append(k)
    return deps

# --- Security: run pip-audit if requested ----------------------------------

def run_pip_audit(paths: List[str]) -> List[str]:
    """
    If pip-audit is installed, run it. This requires network or installed package metadata.
    We return simple text lines (findings).
    """
    if not HAVE_PIP_AUDIT:
        return ["pip-audit not installed; install 'pip-audit' to run vulnerability scans."]
    try:
        from pip_audit import _service as pa_service  # type: ignore
        # High-level API is not always stable; fall back to subprocess if needed.
        # To avoid complex APIs in prototype, use subprocess to call pip-audit CLI if available.
        import subprocess, shlex
        cmd = ["pip-audit", "--format", "text"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout.splitlines() or ["No vulnerable packages found."]
        else:
            return proc.stdout.splitlines() + proc.stderr.splitlines()
    except Exception as e:
        return [f"pip-audit run failed: {e}"]

# --- Main per-file pipeline -------------------------------------------------

def process_file(path: str, args) -> Dict:
    result = {
        "path": path,
        "formatted": False,
        "format_messages": [],
        "lint_messages": [],
        "renames_suggested": {},
        "renames_applied": {},
        "unused_imports": [],
        "errors": [],
    }
    original = read_file(path)
    text = original

    # 1) Lint: whitespace checks
    # detect mixed tabs/spaces
    leading_patterns = set()
    for i, ln in enumerate(original.splitlines(), 1):
        m = re.match(r"^([ \t]+)", ln)
        if m:
            leading_patterns.add(m.group(1))
    # detect presence of both tabs and spaces in leading whitespace across file
    has_tabs = any("\t" in p for p in leading_patterns)
    has_spaces = any(" " in p for p in leading_patterns)
    if has_tabs and has_spaces:
        result["lint_messages"].append("Mixed tabs and spaces in leading indentation (file-level).")
    # detect inconsistent indent widths
    indent_counts = Counter()
    for p in leading_patterns:
        spaces = p.count(" ")
        if spaces:
            indent_counts[spaces] += 1
    if indent_counts:
        common = indent_counts.most_common(1)[0][0]
        # if there are many different indent counts, warn
        if len(indent_counts) > 2:
            result["lint_messages"].append(f"Inconsistent indentation widths detected (common={common}).")

    # 2) Formatting: use black if installed and --no-fix not set
    if args.fix and HAVE_BLACK:
        try:
            new_text = black.format_file_contents(original, fast=False, mode=black.Mode())
            if new_text != original:
                result["formatted"] = True
                result["format_messages"].append("Formatted with black.")
                text = new_text
        except Exception as e:
            result["format_messages"].append(f"Black failed: {e}")
    else:
        # fallback normalizer always runs in non--no-fix mode
        if args.fix:
            new_text, msgs = normalize_whitespace(original)
            if msgs:
                result["formatted"] = True
                result["format_messages"].extend(msgs)
                text = new_text

    # 3) AST analysis for identifiers
    try:
        tree = ast.parse(text)
    except Exception as e:
        result["errors"].append(f"AST parse error: {e}")
        return result

    collector = IdentifierCollector()
    collector.visit(tree)

    # find unused imports
    unused = detect_unused_imports(tree)
    if unused:
        result["unused_imports"] = unused
        if args.fix:
            # attempt to remove unused imports conservatively via simple regex removal
            # NOTE: this is a simplistic approach; better to use libcst for accurate removal.
            new_text = text
            for name in unused:
                # remove "import name" or "from x import name"
                new_text = re.sub(rf"^\s*(from\s+[^\n]+\s+import\s+.*\b{name}\b.*)$", "", new_text, flags=re.MULTILINE)
                new_text = re.sub(rf"^\s*(import\s+.*\b{name}\b.*)$", "", new_text, flags=re.MULTILINE)
            if new_text != text:
                result["format_messages"].append(f"Removed unused import(s): {', '.join(unused)} (heuristic).")
                text = new_text

    # detect identifier typos
    typos = detect_identifier_typos(collector)
    result["renames_suggested"] = typos

    # apply renames if requested and we have libcst
    if args.fix and typos:
        if HAVE_LIBCST:
            try:
                new_text = apply_renames_with_libcst(text, typos)
                result["renames_applied"] = typos.copy()
                text = new_text
                result["format_messages"].append(f"Applied {len(typos)} identifier rename(s) using libcst.")
            except Exception as e:
                result["errors"].append(f"libcst rename failed: {e}")
        else:
            result["lint_messages"].append("libcst not installed; suggested renames not applied. Install 'libcst' to auto-apply renames.")

    # final format pass with black if available & fix mode
    if args.fix and HAVE_BLACK:
        try:
            new_text = black.format_file_contents(text, fast=False, mode=black.Mode())
            if new_text != text:
                result["formatted"] = True
                result["format_messages"].append("Final pass: formatted with black.")
                text = new_text
        except Exception as e:
            result["format_messages"].append(f"Black final pass failed: {e}")

    # Write back if changes and not no-fix
    if args.fix and text != original:
        write_file(path, text)

    return result

# --- CLI / Orchestration ----------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(prog="pymax", description="pymax: formatter + linter + dependency analyzer (prototype)")
    p.add_argument("paths", nargs="*", help="File or directory paths to process (default: .)")
    p.add_argument("--fix", action="store_true", help="Auto-fix issues where possible (formatting, renames if libcst available).")
    p.add_argument("--no-fix", dest="fix", action="store_false", help="Do not modify files; only analyze (default behavior if unspecified).")
    p.add_argument("--secure", action="store_true", help="Run dependency security checks (requires pip-audit).")
    p.add_argument("--strict", action="store_true", help="Treat warnings as errors in exit code.")
    p.add_argument("--report", choices=["text", "json"], default="text", help="Report format.")
    return p.parse_args()

def gather_project_deps(start_paths: List[str]) -> List[str]:
    deps = []
    # look for requirements.txt upwards from each path, and pyproject.toml in root
    visited = set()
    for p in start_paths or ["./"]:
        root = p if os.path.isdir(p) else os.path.dirname(os.path.abspath(p)) or "."
        # check common files in root
        req = os.path.join(root, "requirements.txt")
        deps.extend(parse_requirements_txt(req))
        pyproj = os.path.join(root, "pyproject.toml")
        deps.extend(parse_pyproject_toml_for_deps(pyproj))
        # also check top-level requirements in current working dir
    deps = [d for d in deps if d]
    return sorted(set(deps))

def main():
    args = parse_args()
    files = find_python_files(args.paths)
    if not files:
        print("No python files found.", file=sys.stderr)
        sys.exit(1)

    results = []
    for f in files:
        res = process_file(f, args)
        results.append(res)

    deps = gather_project_deps(args.paths)
    security_findings = []
    if args.secure:
        security_findings = run_pip_audit(args.paths)

    # produce summary
    summary = {
        "files_processed": len(files),
        "files": results,
        "deps_found": deps,
        "security_findings": security_findings,
    }

    # output
    if args.report == "json":
        print(json.dumps(summary, indent=2))
    else:
        print(f"✨ pymax finished: {len(files)} file(s) processed\n")
        total_fixed = 0
        total_warnings = 0
        for r in results:
            print(f"— {r['path']}")
            if r["formatted"]:
                print("  ✔ Formatted code")
            for m in r["format_messages"]:
                print("   •", m)
            for m in r["lint_messages"]:
                print("   ⚠", m)
                total_warnings += 1
            if r["renames_suggested"]:
                print(f"   🔤 Suggested renames ({len(r['renames_suggested'])}):")
                for a,b in r["renames_suggested"].items():
                    applied = " (applied)" if r["renames_applied"].get(a)==b else ""
                    print(f"    - {a}  → {b}{applied}")
                    if applied:
                        total_fixed += 1
            if r["unused_imports"]:
                print("   🧹 Unused imports (heuristic):", ", ".join(r["unused_imports"]))
            if r["errors"]:
                for e in r["errors"]:
                    print("   ❌", e)
        print("\nDependencies found:")
        if deps:
            for d in deps[:50]:
                print("  -", d)
        else:
            print("  (no requirements.txt or pyproject.toml deps detected)")

        if args.secure:
            print("\nSecurity scan:")
            for s in security_findings:
                print(" ", s)

        print(f"\nSummary: files={len(files)}, suggested renames={sum(len(r['renames_suggested']) for r in results)}, warnings={total_warnings}, fixes_applied={total_fixed}")
        if args.strict and total_warnings > 0:
            sys.exit(2)

if __name__ == "__main__":
    main()
