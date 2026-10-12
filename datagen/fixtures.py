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
PIP_PKGS.update({
    "pyyaml": "yaml", "pyjwt": "jwt", "python-dateutil": "dateutil", "flask-cors": "flask_cors", "flask-sqlalchemy": "flask_sqlalchemy", "pyserial": "serial",
    "attrs": "attr", "python-magic": "magic", "pycryptodome": "Crypto", "gitpython": "git", "mysqlclient": "MySQLdb", "python-docx": "docx", "python-pptx": "pptx",
    "tensorflow": "tensorflow", "plotly": "plotly", "streamlit": "streamlit", "selenium": "selenium", "scrapy": "scrapy", "pygame": "pygame", "sympy": "sympy",
    "networkx": "networkx", "nltk": "nltk", "openai": "openai", "psutil": "psutil", "paramiko": "paramiko", "cryptography": "cryptography", "tabulate": "tabulate",
    "colorama": "colorama", "pytz": "pytz", "openpyxl": "openpyxl", "tornado": "tornado", "websockets": "websockets", "pymongo": "pymongo", "alembic": "alembic",
    "marshmallow": "marshmallow", "werkzeug": "werkzeug", "numba": "numba", "xgboost": "xgboost", "tqdm": "tqdm", "docker": "docker", "kubernetes": "kubernetes",
    "pyperclip": "pyperclip", "pexpect": "pexpect", "pytest-mock": "pytest_mock", "sqlmodel": "sqlmodel", "loguru": "loguru", "toml": "toml", "pillow-heif": "pillow_heif",
})
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


# (command, apt package): none of these commands exist in the sandbox image (checked by validate_sim)
APT_TOOLS = [
    ("rg", "ripgrep"), ("tree", "tree"), ("jq", "jq"), ("htop", "htop"), ("vim", "vim"), ("tmux", "tmux"), ("ncdu", "ncdu"), ("sqlite3", "sqlite3"),
    ("ffmpeg", "ffmpeg"), ("rsync", "rsync"), ("lsof", "lsof"), ("strace", "strace"), ("nano", "nano"), ("screen", "screen"), ("ag", "silversearcher-ag"),
    ("nmap", "nmap"), ("traceroute", "traceroute"), ("whois", "whois"), ("dig", "dnsutils"), ("nslookup", "dnsutils"), ("ifconfig", "net-tools"),
    ("netstat", "net-tools"), ("ping", "iputils-ping"), ("mtr", "mtr"), ("iftop", "iftop"), ("iotop", "iotop"), ("tcpdump", "tcpdump"), ("gdb", "gdb"),
    ("valgrind", "valgrind"), ("ninja", "ninja-build"), ("meson", "meson"), ("pandoc", "pandoc"), ("convert", "imagemagick"), ("xclip", "xclip"),
    ("xsel", "xsel"), ("parallel", "parallel"), ("shellcheck", "shellcheck"), ("entr", "entr"), ("fzf", "fzf"), ("ranger", "ranger"), ("mc", "mc"),
    ("7z", "p7zip-full"), ("pigz", "pigz"), ("pv", "pv"), ("bc", "bc"), ("psql", "postgresql-client"), ("mysql", "mysql-client"), ("redis-cli", "redis-tools"),
    ("cowsay", "cowsay"), ("figlet", "figlet"), ("neofetch", "neofetch"), ("go", "golang-go"), ("ruby", "ruby"), ("php", "php-cli"), ("clang", "clang"),
    ("autoconf", "autoconf"), ("pkg-config", "pkg-config"), ("nasm", "nasm"), ("git-lfs", "git-lfs"), ("ccache", "ccache"), ("sshfs", "sshfs"), ("socat", "socat"),
    ("telnet", "telnet"), ("ftp", "ftp"), ("dos2unix", "dos2unix"), ("tig", "tig"), ("colordiff", "colordiff"), ("pdftotext", "poppler-utils"),
    ("exiftool", "libimage-exiftool-perl"), ("zstd", "zstd"), ("lz4", "lz4"), ("mogrify", "imagemagick"), ("bat", "bat"), ("fdfind", "fd-find"),
]
_SKIP_MODULES = {"pytest", "black", "ruff", "flake8", "mypy", "setuptools", "wheel", "build", "twine", "gunicorn", "uvicorn", "jupyter", "torch", "celery"}
PIP_MODULES = [(m, d) for d, m in PIP_PKGS.items() if d not in _SKIP_MODULES and not m.endswith("_stub")]     # (import name, dist name)
# shell words commonly mistyped, with a harmless correct invocation in a project directory: (command, args)
TYPO_CMDS = [("ls", ""), ("ls", "-la"), ("cat", "README.md"), ("pwd", ""), ("clear", ""), ("git", "status"), ("git", "log --oneline"), ("git", "branch"), ("git", "diff"),
             ("git", "remote -v"), ("git", "fetch"), ("python3", "--version"), ("node", "--version"), ("npm", "--version"), ("grep", "-rn hello ."), ("head", "README.md"),
             ("tail", "README.md"), ("wc", "-l README.md"), ("echo", "hello"), ("which", "python3"), ("mkdir", "newdir"), ("touch", "notes.txt"), ("cp", "README.md copy.md"),
             ("mv", "README.md docs.md"), ("less", "README.md"), ("find", ". -name \"*.js\""), ("tar", "czf out.tgz README.md"), ("chmod", "+x build.js"), ("npm", "test"),
             ("npm", "run build"), ("make", "--version"), ("gcc", "--version"), ("cargo", "--version"), ("pip", "--version"), ("zip", "out.zip README.md"), ("curl", "--version")]
NAMED = {"apt_tools": APT_TOOLS, "pip_modules": PIP_MODULES, "typo_cmds": TYPO_CMDS}
