# vyre.py
import ast
import sys
import inspect
import argparse
from types import FunctionType
from typing import get_type_hints

class VyreChecker:
    def __init__(self, filename: str, strict: bool = False, strictness: int = 0):
        self.filename = filename
        self.strict = strict
        self.strictness = strictness
        self.warnings = []
        self.errors = []

    def check(self):
        # Parse the AST of the file
        with open(self.filename, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=self.filename)

        # Compile the file to capture actual objects
        namespace = {}
        exec(compile(tree, filename=self.filename, mode='exec'), namespace)

        # Iterate over functions
        for name, obj in namespace.items():
            if isinstance(obj, FunctionType):
                self._check_function(name, obj)

        # Print results
        for w in self.warnings:
            print(f"WARNING: {w}")
        for e in self.errors:
            print(f"ERROR: {e}")
        if self.errors and self.strict:
            sys.exit(1)

    def _check_function(self, name: str, func: FunctionType):
        # Type hints
        hints = get_type_hints(func)
        # Check return type
        sig = inspect.signature(func)
        try:
            result = func(*[self._dummy_value(param.annotation) for param in sig.parameters.values()])
            if 'return' in hints and not isinstance(result, hints['return']):
                msg = f"Function '{name}' return type potential mismatch: expected {hints['return']}, got {type(result)}"
                self._report(msg, error=True)
        except Exception:
            # Cannot execute function safely, skip
            pass

        # Check variable names in locals for potential overrides
        local_vars = func.__code__.co_varnames
        if len(local_vars) != len(set(local_vars)):
            msg = f"Function '{name}' potential variable name override"
            self._report(msg, error=self.strictness==2)

        # Check function name override in globals
        # (only if strictness >= 1)
        if self.strictness >= 1:
            if name in globals():
                msg = f"Function name '{name}' may override global name"
                self._report(msg, error=False)

        # Special warnings
        if self.strictness >= 2:
            # Simple heuristic for infinite loop: while True in source
            if "while True" in inspect.getsource(func):
                msg = f"Potential infinite loop found in function '{name}'"
                self._report(msg, error=self.strictness==2)

            # Unoptimized code heuristic: multiple assignments without type hints
            assignments = [n for n in ast.walk(ast.parse(inspect.getsource(func))) if isinstance(n, ast.Assign)]
            for a in assignments:
                for target in a.targets:
                    if isinstance(target, ast.Name) and target.id not in func.__annotations__:
                        msg = f"Potential unoptimized code found: '{target.id}' has no type hint in '{name}'"
                        self._report(msg, error=False)

    def _dummy_value(self, hint):
        # Provide dummy value for basic types
        if hint in [int, float]:
            return 0
        elif hint == str:
            return ""
        elif hint == bool:
            return True
        elif hint == list:
            return []
        elif hint == dict:
            return {}
        elif hint == type(None):
            return None
        else:
            return None

    def _report(self, msg: str, error: bool = False):
        if error or (self.strict and error is False):
            self.errors.append(msg)
        else:
            self.warnings.append(msg)

def main():
    parser = argparse.ArgumentParser(description="Vyre: Futuristic Python static type checker")
    parser.add_argument("file", help="Python file to check")
    parser.add_argument("--strict", action="store_true", help="Turn warnings into errors")
    parser.add_argument("--strictness", type=int, choices=[0,1,2], default=0, help="Set strictness level (0-2)")
    args = parser.parse_args()

    checker = VyreChecker(args.file, strict=args.strict, strictness=args.strictness)
    checker.check()

if __name__ == "__main__":
    main()
