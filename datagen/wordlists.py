"""Name generators for scenario expansion: many combinators over word pools, so names don't repeat from one fixed list."""
import random

NOUNS = """api app arch atlas badge bank beacon bench bit blog board bolt book bot box bridge broker bucket buffer build cache calc camera canvas card
cart catalog chart chat checker city clock cloud cluster code compass config console core counter crawler crypto dash data deck deploy desk
diary dice docs dock domain draft drive engine event explorer fabric feed field file filter finder flow forge form forum frame gallery gate
gear gem ghost graph grid guard hub index inbox ink input jar job journal kernel key kit lab ladder lake lamp layer ledger lens library link
list loader lock log loop mail map market mesh metric mirror model monitor mount nest net node note notebook oracle orbit pad page panel parser
path pilot pipe pixel planner plot pocket portal post press probe proxy pulse queue quiz radar rails reader recipe relay render repo report
robot router runner sandbox scanner schema script search sensor server shell shop signal site sketch slate snippet socket solver sound spark
spec stack stream studio suite sync table tagger task tracker trail tree tunnel unit vault viewer voice wallet watcher wave web wiki window
worker wrapper zone""".split()
ADJS = """agile amber ancient bright brisk calm clever cosmic crisp dark deep eager early fast fierce fresh gentle giant golden happy hidden humble
icy jolly keen lazy light lively lone lucky magic mellow mighty modern nimble noble odd pale plain proud quick quiet rapid rare red rusty
sharp silent silver simple sleek small smart smooth solid stable steady swift tidy tiny true vivid warm wild wise young""".split()
SUFFIXES = "api app cli service server web lib utils core bot tool kit sdk engine worker site dashboard backend frontend tracker manager".split()
PREFIXES = "py go js node rust my the super micro mini open easy".split()
FIRST = """alice bob carol dave erin frank grace heidi ivan judy karl linda mike nina oscar peggy quinn ruth sam tina uma victor wendy xena yuri zoe
amir bruno chen diego elena farid gita hugo iris jonas kofi lena marco nadia omar petra rahul sofia tomas ula vera wei yara zane""".split()
TOPICS = "auth login search cache logging parser upload export import metrics retry cleanup refactor typo docs ci tests api ui billing settings".split()
EXTS = {"txt": "notes todo draft readme summary log changelog", "md": "notes todo plan design readme guide", "py": "main app utils helpers server models cli tasks",
        "js": "index app server utils config routes helpers", "c": "main utils parser buffer", "sh": "setup deploy backup build run install",
        "json": "config data settings package manifest", "yaml": "config settings compose values", "csv": "data report users export sales"}
PIP = ["flask", "requests", "numpy", "pandas", "django", "fastapi", "uvicorn", "pytest", "black", "ruff", "pydantic", "sqlalchemy", "httpx", "click", "rich", "tqdm", "jinja2"]
NPM = ["express", "react", "lodash", "axios", "typescript", "vite", "jest", "eslint", "prettier", "dotenv", "cors", "nodemon", "mocha", "chalk", "commander"]
# (command, apt package): commands absent from the sandbox image
APT_TOOLS = [("rg", "ripgrep"), ("tree", "tree"), ("jq", "jq"), ("htop", "htop"), ("vim", "vim"), ("tmux", "tmux"), ("ncdu", "ncdu"), ("sqlite3", "sqlite3"),
             ("ffmpeg", "ffmpeg"), ("rsync", "rsync"), ("lsof", "lsof"), ("strace", "strace"), ("nano", "nano"), ("screen", "screen"), ("ag", "silversearcher-ag")]  # legacy small pool


def pick(r, xs):
    return xs[r.randrange(len(xs))]


def noun(r):
    return pick(r, NOUNS)


def project(r):
    s = r.randrange(8)
    n = noun(r)
    name = {0: f"{pick(r, ADJS)}-{n}", 1: f"{n}-{pick(r, SUFFIXES)}", 2: f"{pick(r, PREFIXES)}-{n}", 3: f"{n}-{noun(r)}", 4: f"{pick(r, ADJS)}_{n}",
            5: f"{n}_{pick(r, SUFFIXES)}", 6: f"{n}-{pick(r, SUFFIXES)}-v{r.randrange(2, 5)}", 7: f"{n}{r.randrange(2, 99)}"}[s]
    return name


def user(r):
    f = pick(r, FIRST)
    return pick(r, [f, f"{f}-dev", f"{f}{r.randrange(1, 99)}", f"{f[0]}{pick(r, FIRST)}", f"{f}-{pick(r, NOUNS)}"])


def branch(r):
    return pick(r, [f"feature/{pick(r, TOPICS)}", f"fix/{pick(r, TOPICS)}-{noun(r)}", f"feat-{noun(r)}", "dev", "develop", f"release-{r.randrange(1, 4)}.{r.randrange(0, 9)}",
                    f"hotfix/{pick(r, TOPICS)}", f"{pick(r, FIRST)}/{pick(r, TOPICS)}", f"wip-{noun(r)}", f"bugfix/{noun(r)}"])


def version(r):
    return f"{r.randrange(0, 4)}.{r.randrange(0, 20)}.{r.randrange(0, 12)}" if r.random() < 0.8 else f"{r.randrange(1, 9)}.{r.randrange(0, 30)}"


def filename(r, ext=None):
    ext = ext or pick(r, sorted(EXTS))
    stem = pick(r, EXTS[ext].split())
    return pick(r, [stem, f"{stem}_{noun(r)}", f"{noun(r)}_{stem}", f"{stem}{r.randrange(2, 9)}", f"{stem}-{noun(r)}"]) + "." + ext


def base_dir(r):
    return pick(r, ["~", "~/projects", "~/dev", "~/code", "~/work", "~/src", f"~/projects/{pick(r, ADJS)}-{noun(r)}", f"~/code/{pick(r, FIRST)}",
                    "~/Documents", "~/Downloads", "~/sandbox", f"~/work/{noun(r)}", "~/repos"])


GENERATORS = {
    "project": project, "word": noun, "user": user, "branch": branch, "version": version, "file": filename,
    "port": lambda r: pick(r, [3000, 3001, 4000, 5000, 5173, 8000, 8080, 8888, 9000]),
    "pip": lambda r: pick(r, PIP), "npm": lambda r: pick(r, NPM), "apt": lambda r: pick(r, APT_TOOLS),
    "adj": lambda r: pick(r, ADJS), "topic": lambda r: pick(r, TOPICS), "msg": lambda r: pick(r, [
        f"fix {pick(r, TOPICS)}", f"add {noun(r)}", f"update {noun(r)}", f"wip {pick(r, TOPICS)}", f"refactor {noun(r)}", f"initial commit",
        f"{pick(r, TOPICS)}: handle {noun(r)}", "cleanup", f"bump {noun(r)} to {version(r)}"]),
}
for _e in EXTS:
    GENERATORS[f"file_{_e}"] = (lambda e: lambda r: filename(r, e))(_e)


def draw(r, spec):
    """spec: {gen: name} | {choice: [...]} | {range: [a, b]} | {fmt: "template"} (fmt is resolved by the caller)"""
    if "gen" in spec:
        return GENERATORS[spec["gen"]](r)
    if "choice" in spec:
        return pick(r, spec["choice"])
    if "range" in spec:
        return r.randrange(spec["range"][0], spec["range"][1] + 1)
    raise ValueError(spec)
