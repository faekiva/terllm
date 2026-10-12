"""Stage 1 of LLM scenario expansion: ask the model for concrete terminal tasks per category.

uv run datagen/llm_tasks.py prompts            -> data/raw/llm/tasks.prompts.jsonl   (upload + run train/llm_gen.py)
uv run datagen/llm_tasks.py parse <out.jsonl>  -> data/raw/llm/tasks.json            ({id, class, category, task})
"""
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "llm"

# class | category | what the commands are about
CATEGORIES = """
routine|git daily flow|checking status, diffs, logs, staging, committing and pushing small changes
routine|git branches|creating, switching, renaming, deleting and comparing branches
routine|git history|searching history: log filters, blame, show, grep, shortlog, reflog
routine|git undo|undoing mistakes: restore, reset, revert, amend, checkout of files, stash
chained|git collaboration|clone, fetch, pull --rebase, rebase, merge, resolve conflicts, tags, release
chained|git advanced|bisect, cherry-pick, worktree, submodules, interactive rebase, sparse checkout
routine|navigating files|moving around directories, listing, tree, finding where things are
routine|file inspection|head, tail, less, cat, wc, file, stat, du on project files
chained|find and act|find with -name/-mtime/-size combined with xargs, -exec, delete or move
chained|text processing|grep, sed, awk, cut, sort, uniq, tr pipelines on logs or CSV files
chained|json and yaml|jq and yq on API responses and config files
routine|archives|tar, zip, unzip, gzip, xz, listing and extracting archives
routine|permissions|chmod, chown, umask, ls -l, making scripts executable, sudo
routine|links and paths|ln -s, readlink, realpath, which, type, PATH problems
routine|disk usage|df, du, ncdu, finding big files, cleaning caches
routine|processes|ps, top, htop, kill, pkill, pgrep, jobs, bg, fg, nohup
routine|system info|uname, uptime, free, lscpu, lsblk, lsb_release, whoami, id, env
routine|environment and dotfiles|export, source, .bashrc/.zshrc edits, aliases, env vars, direnv
routine|shell scripting basics|writing, chmod +x and running small bash scripts, loops, if tests in one-liners
routine|cron and timers|crontab -e, crontab -l, systemd timers, at
routine|systemd services|systemctl status/start/stop/restart/enable, journalctl -u, daemon-reload
routine|logs|tail -f, journalctl -f, grep errors in logs, logrotate, zcat of rotated logs
routine|networking diagnostics|ping, curl -I, dig, nslookup, traceroute, ss -tulpn, nc, ip addr
routine|ports and servers|find what listens on a port, kill it, start a dev server, curl localhost
chained|ssh workflows|ssh to a host, scp, rsync, ssh-keygen, ssh-copy-id, ~/.ssh/config, tunnels
chained|remote deploy|rsync to a server, ssh restart service, tail logs, rollback
chained|curl and APIs|curl GET/POST with headers and JSON, saving responses, jq on results, auth tokens in env vars
chained|python project setup|python3 -m venv, activate, pip install, requirements, run the app
chained|python testing|pytest with flags, -k, -x, coverage, fixing failing tests, running one file
chained|python tooling|ruff, black, mypy, pip freeze, pip list --outdated, build, twine, uv
chained|django flask fastapi|manage.py migrate/runserver, flask run, uvicorn app:app --reload
chained|jupyter and data science|jupyter, pandas scripts, python scripts with args, nvidia-smi, running training scripts, tensorboard
chained|node project setup|npm init/install, npm run scripts, npx, package.json edits, nvm use
chained|node testing and build|npm test, npm run build, jest, eslint, prettier, vite, next
chained|yarn pnpm|yarn add, pnpm install, workspaces, lockfile conflicts
chained|rust cargo|cargo new/build/run/test/clippy/fmt/add/doc, release builds, target dir cleanup
chained|go projects|go mod init/tidy, go build/run/test, go vet, gofmt
chained|c and cpp builds|gcc/g++ with flags, make, cmake -B build, ninja, running binaries, gdb, valgrind
chained|java and gradle|javac, java -jar, mvn clean install, gradle build, ./gradlew test
chained|make and tasks|make targets, make -j, make clean, just/Taskfile, running scripts
chained|docker images|docker build -t, docker images, docker tag, docker push, docker rmi, prune
chained|docker containers|docker run -d -p, docker ps, docker logs -f, docker exec -it, docker stop/rm, volumes
chained|docker compose|docker compose up -d, logs, ps, down, build, exec, env files
chained|kubernetes|kubectl get/describe/logs/exec/apply/delete/rollout, contexts, namespaces, port-forward
chained|terraform and cloud cli|terraform init/plan/apply, aws s3 ls/cp, aws sts get-caller-identity, gcloud config
chained|databases|psql -c, sqlite3 db .tables, mysql -e, redis-cli keys/get, pg_dump, backups
chained|package managers apt|apt update, apt install, apt search, apt remove, dpkg -l, apt list --upgradable
chained|package managers misc|brew install, snap install, pipx, cargo install, go install, npm -g
routine|editors|opening files with vim, nano, code ., less, editing configs with sudo
routine|tmux and screen|tmux new -s, attach, ls, kill-session, split panes, screen -r
chained|media tools|ffmpeg convert/trim/extract audio, imagemagick convert/mogrify, yt-dlp
routine|text and docs|pandoc, markdown, wc -w, spell, diff of documents, cat >> notes
routine|clipboard and misc|xclip, date, cal, bc, seq, watch, sleep, timeout, history, clear
routine|backup and sync|rsync -av, tar backup, cp -a, rclone, dd if=of=, checksum with sha256sum
routine|users and groups|useradd, usermod -aG, passwd, groups, su, sudo -l, visudo
routine|firewall and security|ufw status/allow, iptables -L, fail2ban-client, openssl, gpg, ssh-keygen -t ed25519
chained|openssl and certs|openssl req/x509, checking cert expiry, s_client, certbot
routine|python one-liners|python3 -c, python3 -m http.server, python3 -m json.tool, pip show
chained|llm and ml tooling|huggingface-cli download, ollama run/pull/list, llama.cpp ./llama-cli -m, git lfs, wandb
chained|ci debugging|gh run list/view, gh pr create/checkout/merge, gh issue, act, reading build logs
chained|code review flow|git fetch, gh pr checkout, git diff main...HEAD, running tests, git switch back
state_action|dirty repo next steps|the working tree has modified/untracked files and the user decides what to do next
state_action|fresh clone next steps|just cloned a repo: read the README, install dependencies, run tests, start the app
state_action|after failed build|a build or test just failed: read the log, fix the config, rerun
state_action|detached or conflicted git|merge conflict, rebase in progress, detached HEAD: continue, abort or fix
state_action|new empty directory|empty directory: initialize a project of some kind and make the first commit
state_action|downloads folder cleanup|files in ~/Downloads: unzip, move, rename, delete, sort by type
state_action|log file triage|a large log file: find the problem lines, count, tail, extract a time window
state_action|release time|bumping version, tagging, building, publishing, changelog
error_fix|typos in commands|a mistyped command or subcommand followed by the correct one
error_fix|permission problems|permission denied, then chmod/sudo/chown fix
error_fix|command not found|a missing tool, installing it with the right package manager, retrying
error_fix|wrong directory|running a command in the wrong directory, cd to the right one, retry
error_fix|missing python modules|ModuleNotFoundError, pip install into the right venv, retry
error_fix|git mistakes|wrong branch commit, forgot to add, push rejected, no upstream, fixed with the right command
error_fix|port in use|address already in use, find the process, kill it, restart
error_fix|disk or file errors|no space left, file exists, not a directory, fixing and retrying
error_fix|ssh and network errors|connection refused, permission denied (publickey), host key changed, fixing and retrying
error_fix|quoting and globbing mistakes|unmatched quotes, spaces in file names, unexpanded globs, fixed with quoting
""".strip().split("\n")


def categories():
    return [tuple(l.split("|")) for l in CATEGORIES]


def prompt(cls, cat, desc, n=40):
    return (f"List {n} different, concrete things a developer or sysadmin does in a Linux terminal in this area: {cat} ({desc}).\n"
            "One task per line, 6-14 words each, phrased as a short goal with specifics (tool names, file kinds, services), no numbering, no commentary. "
            "Cover common everyday cases first, then less common ones. Vary the tools and the situations.")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if sys.argv[1] == "prompts":
        with open(OUT / "tasks.prompts.jsonl", "w") as f:
            for i, (cls, cat, desc) in enumerate(categories()):
                for rep in range(6):                                  # 6 samples per category at temperature 1.0 -> ~240 raw tasks each
                    f.write(json.dumps({"pid": f"{i}.{rep}", "cls": cls, "cat": cat, "prompt": prompt(cls, cat, desc)}) + "\n")
        print(len(categories()), "categories")
    else:
        tasks, seen = [], set()
        for line in open(sys.argv[2]):
            o = json.loads(line)
            for t in o["text"].split("\n"):
                t = re.sub(r"^[\s\-*\d.)]+", "", t).strip().rstrip(".")
                key = re.sub(r"\W+", " ", t.lower()).strip()
                if not (20 <= len(t) <= 140) or key in seen:
                    continue
                seen.add(key)
                tasks.append({"id": hashlib.blake2b(key.encode(), digest_size=5).hexdigest(), "class": o["cls"], "category": o["cat"], "task": t})
        json.dump(tasks, open(OUT / "tasks.json", "w"), indent=0)
        print(len(tasks), "unique tasks")


if __name__ == "__main__":
    main()
