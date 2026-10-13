"""Tests for lineparse: run with `python3 runtime/test_lineparse.py`.

Token lists are the real ones from the shadow log of a sandbox run.
"""
import unittest

import lineparse as lp

THR = 0.8


class ParseTest(unittest.TestCase):
    def test_chained_command_uses_current_segment(self):
        # given a chain whose cursor sits after `cd `
        words = ["mkdir", "beep", "&&", "cd"]
        # when the line is parsed
        line = lp.parse("mkdir beep && cd ", words)
        # then the segment, command and created dir reflect only what zsh would run
        self.assertEqual(line.segment, "cd ")
        self.assertEqual(line.cmd, "cd")
        self.assertEqual(line.cur, "")
        self.assertFalse(line.simple)
        self.assertEqual(line.created, ["beep/"])

    def test_precommands_and_assignments_are_skipped(self):
        # given a command behind sudo and an env assignment
        line = lp.parse("FOO=1 sudo cat /et", ["FOO=1", "sudo", "cat", "/et"])
        # then the command is cat and the line is still simple
        self.assertEqual((line.cmd, line.cur, line.simple), ("cat", "/et", True))

    def test_typing_the_command_name_has_no_cmd(self):
        line = lp.parse("ca", ["ca"])
        self.assertEqual((line.cmd, line.cur), ("", "ca"))

    def test_mkdir_p_and_git_clone_create_dirs(self):
        # given dirs made by mkdir -p and both git clone forms
        words = ["mkdir", "-p", "src/lib", "&&", "git", "clone", "https://x/y/tool.git",
                 "&&", "git", "clone", "u", "dest", "&&", "cd", "s"]
        line = lp.parse("… cd s", words)
        # then all three are created-dir candidates
        self.assertEqual(line.created, ["src/lib/", "tool/", "dest/"])

    def test_fallback_without_zsh_words(self):
        self.assertEqual(lp.parse("cd ../m").cur, "../m")


class CandidatesTest(unittest.TestCase):
    def test_created_dirs_matching_cur_are_added(self):
        # given `beep/` created earlier and no matching dirs on disk
        line = lp.parse("mkdir beep && cd b", ["mkdir", "beep", "&&", "cd", "b"])
        # then it becomes the candidate
        self.assertEqual(lp.candidates(line, []), ["beep/"])

    def test_nonmatching_and_duplicate_dirs_are_not_added(self):
        line = lp.parse("mkdir beep && cd x", ["mkdir", "beep", "&&", "cd", "x"])
        self.assertEqual(lp.candidates(line, ["xs/"]), ["xs/"])


class DecideTest(unittest.TestCase):
    def test_rule1_shows_low_confidence_word_that_is_a_candidate(self):
        # given the model wrote auth.py but doubted its first token
        toks = [("th", 0.311), (".", 0.965), ("py", 1.0)]
        line = lp.parse("vim au", ["vim", "au"])
        # when deciding
        shown, rule = lp.decide(toks, THR, line, ["auth.py"])
        # then the candidate-backed word is shown
        self.assertEqual((shown, rule), ("th.py", "backed"))

    def test_rule1_ignores_words_that_are_not_candidates(self):
        # given the model wrote aut.py, which doesn't exist
        toks = [("t", 0.409), (".", 0.169), ("py", 1.0)]
        line = lp.parse("python3 au", ["python3", "au"])
        # then nothing is shown, as before
        self.assertEqual(lp.decide(toks, THR, line, ["auth.py"]), ("", ""))

    def test_rule1_shows_up_to_a_candidate_dir_the_word_goes_into(self):
        # given the model is sure of `proj` but not of what follows `proj/`
        toks = [("oj", 0.998), ("/", 0.747), ("mk", 0.262)]
        line = lp.parse("cd pr", ["cd", "pr"])
        # then the candidate dir is shown
        self.assertEqual(lp.decide(toks, THR, line, ["proj/"]), ("oj/", "backed"))

    def test_rule1_matches_dirs_without_trailing_slash(self):
        toks = [("beep", 0.5)]
        line = lp.parse("mkdir beep && cd ", ["mkdir", "beep", "&&", "cd"])
        self.assertEqual(lp.decide(toks, THR, line, ["beep/"]), ("beep", "backed"))

    def test_rule2_vetoes_confident_guess_that_does_not_exist(self):
        # given `cd ../m` with only ../mk/ on disk and a confident `main`
        line = lp.parse("cd ../m", ["cd", "../m"])
        # then the guess is dropped
        self.assertEqual(lp.decide([("ain", 0.944)], THR, line, ["../mk/"]),
                         ("", "vetoed"))

    def test_rule2_keeps_paths_the_listing_cannot_offer(self):
        # given `cd ` and a confident `..` (never in a cwd listing)
        line = lp.parse("cd ", ["cd"])
        # then it survives
        self.assertEqual(lp.decide([("..", 0.9)], THR, line, ["src/"]), ("..", ""))

    def test_rule2_allows_descending_into_a_candidate(self):
        line = lp.parse("cd pr", ["cd", "pr"])
        toks = [("oj/node", 0.9)]
        self.assertEqual(lp.decide(toks, THR, line, ["proj/"]), ("oj/node", ""))

    def test_rule2_skips_chains(self):
        # given a chain (an earlier command may create the path)
        line = lp.parse("tar xf a.tgz && cd fo", ["tar", "xf", "a.tgz", "&&", "cd", "fo"])
        # then a non-candidate guess is kept
        self.assertEqual(lp.decide([("o", 0.95)], THR, line, ["fob/"]), ("o", ""))

    def test_rule2_skips_commands_that_take_new_names(self):
        line = lp.parse("mkdir ne", ["mkdir", "ne"])
        self.assertEqual(lp.decide([("w", 0.95)], THR, line, ["nest/"]), ("w", ""))

    def test_rule2_skips_when_there_are_no_candidates(self):
        line = lp.parse("cd x", ["cd", "x"])
        self.assertEqual(lp.decide([("yz", 0.95)], THR, line, []), ("yz", ""))


if __name__ == "__main__":
    unittest.main()
