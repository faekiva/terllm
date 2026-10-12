"""Fixture templates shared by the simulator and the Docker helpers.

Both sides materialize the exact same project trees, so the simulator and the
real sandbox start from identical state.
"""
import json

# pip dist name -> import name; every entry gets a stub wheel in the sandbox wheelhouse
PIP_PKGS = {
    "flask": "flask", "requests": "requests", "numpy": "numpy", "pandas": "pandas",
    "django": "django", "fastapi": "fastapi", "uvicorn": "uvicorn", "pytest": "pytest",
    "black": "black", "ruff": "ruff", "flake8": "flake8", "mypy": "mypy",
    "pydantic": "pydantic", "sqlalchemy": "sqlalchemy", "httpx": "httpx",
    "pillow": "PIL", "pyyaml": "yaml", "scikit-learn": "sklearn", "beautifulsoup4": "bs4",
    "opencv-python": "cv2", "matplotlib": "matplotlib", "scipy": "scipy",
    "click": "click", "rich": "rich", "tqdm": "tqdm", "jinja2": "jinja2",
    "gunicorn": "gunicorn", "celery": "celery", "redis": "redis", "boto3": "boto3",
    "python-dotenv": "dotenv", "typer": "typer", "pytest-cov": "pytest_cov",
    "setuptools": "setuptools_stub", "wheel": "wheel_stub", "build": "build", "twine": "twine",
    "torch": "torch", "transformers": "transformers", "psycopg2-binary": "psycopg2",
    "aiohttp": "aiohttp", "lxml": "lxml", "seaborn": "seaborn", "jupyter": "jupyter",
}
CONSOLE_SCRIPTS = ["pytest", "black", "ruff", "flake8", "mypy", "uvicorn", "gunicorn", "django-admin"]
NPM_PKGS = [
    "express", "react", "react-dom", "lodash", "axios", "typescript", "vite", "webpack",
    "jest", "eslint", "prettier", "dotenv", "cors", "nodemon", "mocha", "chalk", "commander",
    "next", "vue", "moment", "uuid", "body-parser", "mongoose", "socket.io", "tailwindcss",
]
PYTHON_STDLIB_OK = {"os", "sys", "json", "re", "math", "time", "random", "pathlib", "argparse", "collections"}

LANGS = ["python", "node", "c", "cmake", "rust", "docs"]


def project_files(lang: str, name: str) -> dict[str, str]:
    readme = {"README.md": f"# {name}\n"}
    if lang == "python":
        return {**readme, ".gitignore": ".venv/\n__pycache__/\n", "requirements.txt": "requests\n",
                "app.py": "import requests\nprint('ok')\n", "tests/test_app.py": "def test_ok():\n    assert True\n"}
    if lang == "node":
        pkg = {"name": name, "version": "1.0.0",
               "scripts": {"start": "node index.js", "test": "node test.js", "build": "node build.js"},
               "dependencies": {"express": "^4.18.2"}}
        return {**readme, ".gitignore": "node_modules/\ndist/\n", "package.json": json.dumps(pkg, indent=2) + "\n",
                "index.js": "console.log('hello');\n", "test.js": "console.log('ok');\n",
                "build.js": "const fs = require('fs');\nfs.mkdirSync('dist', {recursive: true});\nfs.writeFileSync('dist/bundle.js', '//');\n"}
    if lang == "c":
        return {**readme, ".gitignore": "main\n", "main.c": '#include <stdio.h>\nint main(void){puts("hi");return 0;}\n',
                "Makefile": "CC=gcc\nall: main\nmain: main.c\n\t$(CC) -o main main.c\nclean:\n\trm -f main\n.PHONY: all clean\n"}
    if lang == "cmake":
        return {**readme, ".gitignore": "build/\n", "main.c": '#include <stdio.h>\nint main(void){puts("hi");return 0;}\n',
                "CMakeLists.txt": f"cmake_minimum_required(VERSION 3.10)\nproject({name} C)\nadd_executable(app main.c)\n"}
    if lang == "rust":
        return {**readme, ".gitignore": "/target\n",
                "Cargo.toml": f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n',
                "src/main.rs": 'fn main() { println!("hi"); }\n'}
    return {**readme, "LICENSE": "MIT\n", "docs/index.md": f"# {name} docs\n"}


def c_main():
    return project_files("c", "x")["main.c"]
