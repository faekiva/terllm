"""Smoke checks for the simulator's context-critical behaviours: uv run datagen/test_sim.py"""
import sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from sim.shell import Sim


def run(sim, *cmds):
    return [sim.run(c) for c in cmds]


def test():
    s = Sim("zsh")
    run(s, "mkdir new-api")
    assert s.run("cd new-api").code == 0 and s.snapshot()["cwd"] == "~/new-api"            # created -> use
    s = Sim("bash")
    run(s, "fx_remote alice/foo python", "git clone https://github.com/alice/foo")
    assert "foo" in s.snapshot()["ls"] and s.run("cd foo").code == 0                          # clone -> cd
    run(s, "git checkout -b feat", "echo x >> README.md", "git commit -am wip")
    r = s.run("git push")
    assert r.code == 128 and "git push --set-upstream origin feat" in r.err                   # missing upstream, real git text
    assert s.run("git push -u origin feat").code == 0 and s.snapshot()["git"]["upstream"] == "origin/feat"
    assert s.run("rg x").code == 127 and "command not found" in s.run("rg x").err             # error comes from the corpus
    run(s, "python3 -m venv .venv", "source .venv/bin/activate")
    assert s.snapshot()["env"] == {"VIRTUAL_ENV": ".venv"} and s.run("pip install requests").code == 0
    assert s.run("echo y >> README.md").code == 0 and s.snapshot()["git"]["dirty"] == [" M README.md"]   # state -> action
    print("sim ok")


def test_fs():
    import render
    s = Sim("zsh")
    run(s, "mkdir -p ~/projects/app/src/components ~/projects/api", "cd ~/projects/app", "touch src/components/Button.tsx build.sh .env README.md", "chmod +x build.sh")
    fs = s.fs_for("vim src/components/Bu")
    assert fs["~/projects/app/src"] == ["components/"] and fs["~/projects/app"][:2] == ["build.sh*", ".env"]
    c = lambda t, b=("python", "pytest", "ls"): render.fs_cands(t, "~/projects/app", fs.get, b)
    assert c("vim src/components/Bu") == ["src/components/Button.tsx"] and c("cd src/") == ["src/components/"]
    assert c("./b") == ["./build.sh"] and c("./") == ["./build.sh", "./src/"]                      # command position: executables and dirs only
    assert c("cat ") == ["build.sh", "README.md", "src/"] and c("cat .") == [".env"] and c("cat -") == []   # dot files only on request, flags never
    assert c("cd ") == ["src/"] and c("pyt") == ["pytest", "python"] and c("") == [] and c("git commit -m 'x") == []
    assert c("ls | py") == ["pytest", "python"] and render.fs_cands("cd ../a", "~/projects/app", {"~/projects": ["api/", "app/"]}.get, ()) == ["../api/", "../app/"]
    print("fs ok")


if __name__ == "__main__":
    test()
    test_fs()
