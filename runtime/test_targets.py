"""Tests for targets: run with `python3 runtime/test_targets.py`."""
import json
import unittest

import lineparse as lp
import targets

MAKEFILE = "VAR := x\n.PHONY: all\ndeploy-staging:\n\techo\nbench-all: deps\n\techo\n%.o: %.c\n"
JUSTFILE = "set shell := [\"zsh\"]\nbuild target=\"x\":\n  echo\n@lint:\n  echo\n"
PKG = json.dumps({"scripts": {"dev": "vite", "lint:fix": "eslint", "test:e2e": "pw"}})
TASK = json.dumps({"tasks": [{"name": "deploy-gaia"}, {"name": "wiki:gen-llms"}]})


def reader(files):
    return lambda path: files.get(path.rsplit("/", 1)[-1])


class RunnerTest(unittest.TestCase):
    def test_target_position_per_runner(self):
        # given the finished words of a segment, then the runner is recognised
        cases = {("make",): "make", ("make", "-j4"): "make", ("npm", "run"): "npm",
                 ("pnpm",): "npm", ("task",): "task", ("npm",): "",
                 ("make", "deploy-staging"): "", ("git",): ""}
        for words, want in cases.items():
            self.assertEqual(targets.runner_for(list(words)), want, words)


class TargetsTest(unittest.TestCase):
    def test_makefile_targets_skip_variables_specials_and_patterns(self):
        got = targets.targets("make", "/p", read=reader({"Makefile": MAKEFILE}))
        self.assertEqual(got, ["deploy-staging", "bench-all"])

    def test_justfile_recipes_with_params_and_quiet_prefix(self):
        got = targets.targets("just", "/p", read=reader({"justfile": JUSTFILE}))
        self.assertEqual(got, ["build", "lint"])

    def test_package_json_scripts(self):
        got = targets.targets("npm", "/p", read=reader({"package.json": PKG}))
        self.assertEqual(got, ["dev", "lint:fix", "test:e2e"])

    def test_taskfile_via_injected_runner(self):
        # given a stubbed `task --list-all --json`
        calls = []
        run = lambda argv, cwd: calls.append((argv, cwd)) or TASK
        # when listing task targets
        got = targets.targets("task", "/p", run=run)
        # then the CLI was asked in the project dir and its names returned
        self.assertEqual(got, ["deploy-gaia", "wiki:gen-llms"])
        self.assertEqual(calls, [(["task", "--list-all", "--json"], "/p")])

    def test_broken_inputs_give_no_targets(self):
        self.assertEqual(targets.targets("npm", "/p", read=reader({"package.json": "{"})), [])
        self.assertEqual(targets.targets("task", "/p", run=lambda a, c: "nope"), [])
        self.assertEqual(targets.targets("make", "/p", read=reader({})), [])

    def test_candidates_filter_by_typed_prefix(self):
        got = targets.candidates(["npm", "run"], "li", "/p", read=reader({"package.json": PKG}))
        self.assertEqual(got, ["lint:fix"])


class StrictDecideTest(unittest.TestCase):
    def test_invented_target_is_vetoed_even_in_a_chain(self):
        # given `cd x && make deploy-` and a confident non-existent target
        line = lp.parse("cd x && make deploy-", ["cd", "x", "&&", "make", "deploy-"])
        # when the targets are the complete list (strict)
        got = lp.decide([("prod", 0.95)], 0.8, line, ["deploy-staging", "deploy-gaia"],
                        strict=True)
        # then the guess is dropped
        self.assertEqual(got, ("", "vetoed"))

    def test_low_confidence_real_target_is_shown(self):
        line = lp.parse("make d", ["make", "d"])
        got = lp.decide([("eploy", 0.4), ("-staging", 0.99)], 0.8, line,
                        ["deploy-staging", "deploy-prod"], strict=True)
        self.assertEqual(got, ("eploy-staging", "backed"))

    def test_unique_target_is_completed_whatever_the_model_says(self):
        # given only deploy-staging starts with `de` and the model wants `default`
        line = lp.parse("make de", ["make", "de"])
        got = lp.decide([("f", 1.0), ("ault", 0.2)], 0.8, line, ["deploy-staging"], strict=True)
        # then the unique target is shown
        self.assertEqual(got, ("ploy-staging", "unique"))

    def test_no_matching_target_vetoes_any_guess(self):
        line = lp.parse("make deploy-p", ["make", "deploy-p"])
        self.assertEqual(lp.decide([("ath", 1.0)], 0.8, line, [], strict=True), ("", "vetoed"))

    def test_unique_rule_does_not_apply_to_files(self):
        # given one file candidate (not strict): the user may be naming a new file
        line = lp.parse("vim ne", ["vim", "ne"])
        self.assertEqual(lp.decide([("w.txt", 0.5)], 0.8, line, ["nest.py"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
