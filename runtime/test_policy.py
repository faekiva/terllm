"""Tests for policy.parse_healed: run with `python3 runtime/test_policy.py`."""
import math
import unittest

import policy


def tok(t, p, top=()):
    return {"token": t, "logprob": math.log(p),
            "top_logprobs": [{"token": a, "logprob": math.log(b)} for a, b in top]}


class ParseHealedTest(unittest.TestCase):
    def test_forced_tokens_are_dropped_and_crossing_token_renormalised(self):
        # given a healed `au` whose output split as a|uth|.|py (real llama-server data)
        r = {"completion_probabilities": [
            tok("a", 0.263, [("a", 0.263), ("~", 0.156), ("auth", 0.147)]),
            tok("uth", 0.001, [(".", 0.999), ("uth", 0.001)]),
            tok(".", 1.0), tok("py", 1.0), tok("", 1.0)]}
        # when parsed
        out = policy.parse_healed(r, "au")
        # then only the new text remains, and `th` is certain among `u…` tokens
        self.assertEqual([t for t, _ in out], ["th", ".", "py"])
        self.assertAlmostEqual(out[0][1], 1.0)

    def test_crossing_token_shares_probability_with_other_allowed_tokens(self):
        # given two allowed continuations of `che`
        r = {"completion_probabilities": [
            tok("check", 0.06, [("ch", 0.5), ("check", 0.06), ("cherry", 0.02)]),
            tok("out", 1.0)]}
        out = policy.parse_healed(r, "che")
        # then `ck` is renormalised over the three tokens consistent with `che`
        self.assertEqual(out[0][0], "ck")
        self.assertAlmostEqual(out[0][1], 0.06 / 0.58, places=3)

    def test_word_ending_on_a_token_boundary_keeps_raw_probabilities(self):
        # given the forced word is exactly one token
        r = {"completion_probabilities": [tok("up", 0.04), tok(" -d", 0.9)]}
        # then the next token is reported as-is
        [(t, p)] = policy.parse_healed(r, "up")
        self.assertEqual(t, " -d")
        self.assertAlmostEqual(p, 0.9)

    def test_without_heal_matches_parse(self):
        r = {"completion_probabilities": [tok("p", 0.97), tok("<|end|>", 1.0)]}
        self.assertEqual(policy.parse_healed(r, ""), policy.parse(r))


if __name__ == "__main__":
    unittest.main()
