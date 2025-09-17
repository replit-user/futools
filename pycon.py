import subprocess
import sys
import os
import ast
import shutil

def check_types(file_path: str) -> None:
    result = subprocess.run(['python3', '-m', 'mypy', '--strict', file_path], capture_output=True)
    if result.returncode != 0:
        print(result.stdout.decode())
        sys.exit(1)

def check_dynamic_imports(file_path: str) -> None:
    with open(file_path, 'r') as f:
        tree = ast.parse(f.read(), file_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == '__import__':
                print('Dynamic __import__ found!')
                sys.exit(1)
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == 'import_module':
                    print('Dynamic import_module found!')
                    sys.exit(1)

def compile_with_nuitka(file_path: str):
    subprocess.run(['nuitka', '--standalone', '--onefile', file_path])
    cleanup_folders(file_path.removesuffix('.py'))

def cleanup_folders(filename):
    dirs_to_remove = [f'{filename}.dist', f'{filename}.build', f'{filename}.onefile-build', '.mypy_cache']
    for dir_path in dirs_to_remove:
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path, ignore_errors=True)
if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('Usage: pyx.py <python_file>')
        sys.exit(1)
    main_file = sys.argv[1]
    check_types(main_file)
    check_dynamic_imports(main_file)
    compile_with_nuitka(main_file)
    print('Compilation complete!')