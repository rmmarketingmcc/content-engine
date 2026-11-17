"""scripts/content_engine_feedback.py --auto keeps reading feedback on an all-approved batch (no network),
and reads the doc by the posts' own headings.

An hourly run that exits as soon as every post in the batch is approved or dropped never reads the author's own
edits in the doc, mail replies and calls about that batch. The script's __main__ block runs here against a
throwaway batch file, with the doc, Gmail, ElevenLabs and side jobs replaced by recorders.

Run: .venv/bin/python -m unittest scripts/tests/test_content_engine_feedback_auto.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "content_engine_feedback.py"
MAIN = 'if __name__ == "__main__":'


def load():
    """The module's functions (everything above the __main__ block) and the block itself."""
    src = SCRIPT.read_text(encoding="utf-8")
    head, _, body = src.partition(MAIN)
    ns = {"__name__": "content_engine_feedback_under_test", "__file__": str(SCRIPT)}
    exec(compile(head, str(SCRIPT), "exec"), ns)
    block = "if True:" + body
    return ns, compile(block, str(SCRIPT), "exec")


class AllApprovedBatch(unittest.TestCase):
    def run_auto(self, posts: list, edits=(), items=()):
        ns, block = load()
        calls = []
        ns.update({
            "side_jobs": lambda: calls.append("side_jobs"),
            "doc_edits": lambda name, p: calls.append(("doc_edits", name)) or list(edits),
            "mail_feedback": lambda name, seen: calls.append(("mail", name)) or list(items),
            "call_feedback": lambda which, seen: calls.append(("call", which)) or [],
            "decide": mock.Mock(side_effect=AssertionError("no feedback, so no model call")),
        })
        with tempfile.TemporaryDirectory() as d:
            ns["BATCHES"] = Path(d)  # active_batches() reads only this folder, never the real batches
            batch = Path(d) / "2026-10-27_Engine_Batch_9.json"
            batch.write_text(json.dumps(posts))
            argv = ["content_engine_feedback.py", "--auto", "--dry-run", "--batch", str(batch)]
            with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as end:
                exec(block, ns)
            self.assertIn(end.exception.code, (0, None))
            after = json.loads(batch.read_text())
        return calls, after

    def test_doc_edits_mail_and_calls_are_still_read(self):
        posts = [{"title": "a", "text": "Post et", "status": "approved"},
                 {"title": "b", "text": "Post to", "status": "dropped"}]
        calls, after = self.run_auto(posts)
        self.assertEqual(calls, ["side_jobs", ("doc_edits", "Engine Batch 9"), ("mail", "Engine Batch 9"), ("call", "latest")])
        self.assertEqual(after, posts)  # nothing to learn, nothing changed


def para(start: int, text: str, style: str = "NORMAL_TEXT", image: bool = False) -> dict:
    elems = [{"inlineObjectElement": {"inlineObjectId": "kix.1"}}] if image else [{"textRun": {"content": text + "\n"}}]
    end = start + len(text) + 1
    return {"startIndex": start, "endIndex": end, "paragraph": {"paragraphStyle": {"namedStyleType": style}, "elements": elems}}


def batch_tab() -> dict:
    """A real tab shape: images under a post and lone marks in HEADING_1 style."""
    rows, i = [], 1
    for text, style, image in [("Batch · written by the engine", "NORMAL_TEXT", False),
                               ("✅ 1 · First post", "HEADING_1", False), ("meta 1", "NORMAL_TEXT", False),
                               ("Linje et", "NORMAL_TEXT", False), ("Linje to", "NORMAL_TEXT", False),
                               ("", "NORMAL_TEXT", True), ("✅", "HEADING_1", False), ("", "HEADING_1", True),
                               ("2 · Second post", "HEADING_1", False), ("meta 2", "NORMAL_TEXT", False),
                               ("Anden post", "NORMAL_TEXT", False), ("", "HEADING_1", True)]:
        rows.append(para(i, text, style, image))
        i = rows[-1]["endIndex"]
    return {"tabs": [{"tabProperties": {"title": "Engine Batch 9", "tabId": "t.9"}, "documentTab": {"body": {"content": rows}}}]}


class FakeDocs:
    def __init__(self, doc):
        self.doc, self.requests = doc, []

    def documents(self):
        return self

    def get(self, **kw):
        return types.SimpleNamespace(execute=lambda: self.doc)

    def batchUpdate(self, documentId, body):
        self.requests += body["requests"]
        return types.SimpleNamespace(execute=lambda: {})


class DocShape(unittest.TestCase):
    def test_images_and_lone_marks_in_heading_style_are_not_posts(self):
        ns, _ = load()
        ns["_docs"] = lambda: FakeDocs(batch_tab())
        self.assertEqual(ns["doc_texts"]("Engine Batch 9"), {1: "Linje et\nLinje to", 2: "Anden post"})
        posts = [{"text": "Linje et\nLinje to", "status": "approved"}, {"text": "Anden post", "status": "approved"}]
        self.assertEqual(ns["doc_edits"]("Engine Batch 9", posts), [])

    def test_a_decision_marks_the_real_heading(self):
        ns, _ = load()
        fake = FakeDocs(batch_tab())
        ns["_docs"] = lambda: fake
        posts = [{"text": "Linje et\nLinje to"}, {"text": "Anden post"}]
        ns["apply_in_doc"]("Engine Batch 9", posts, [{"post": 2, "decision": "approve", "new_text": ""}])
        heading_2 = next(r for r in batch_tab()["tabs"][0]["documentTab"]["body"]["content"]
                         if r["paragraph"]["elements"][0].get("textRun", {}).get("content", "").startswith("2 ·"))
        self.assertEqual(fake.requests, [{"insertText": {"location": {"index": heading_2["startIndex"], "tabId": "t.9"},
                                                         "text": "✅ "}}])

    def test_an_image_marker_in_heading_style_is_not_a_post(self):
        """A '[[IMG name]]' marker left behind by a failed or killed image swap keeps the HEADING_1 style of the
        paragraph it was inserted into. Counting it as a post shifted every later post by one."""
        rows, i = [], 1
        for text, style in [("✅ 1 · Post one", "HEADING_1"), ("meta 1", "NORMAL_TEXT"), ("Post one line", "NORMAL_TEXT"),
                            ("[[IMG post1_1]]", "HEADING_1"),
                            ("2 · Post two", "HEADING_1"), ("meta 2", "NORMAL_TEXT"), ("Post two line", "NORMAL_TEXT"),
                            ("✅ 3 · Post three", "HEADING_1"), ("meta 3", "NORMAL_TEXT"), ("Post three line", "NORMAL_TEXT")]:
            rows.append(para(i, text, style))
            i = rows[-1]["endIndex"]
        doc = {"tabs": [{"tabProperties": {"title": "Engine Batch 9", "tabId": "t.9"},
                         "documentTab": {"body": {"content": rows}}}]}
        ns, _ = load()
        fake = FakeDocs(doc)
        ns["_docs"] = lambda: fake
        self.assertEqual(ns["doc_texts"]("Engine Batch 9"), {1: "Post one line", 2: "Post two line", 3: "Post three line"})
        posts = [{"text": "Post one line", "status": "approved"}, {"text": "Post two line", "status": "approved"},
                 {"text": "Post three line", "status": "approved"}]
        self.assertEqual(ns["doc_edits"]("Engine Batch 9", posts), [])
        # a decision on post 2 marks post 2's own heading, not the marker
        ns["apply_in_doc"]("Engine Batch 9", posts, [{"post": 2, "decision": "approve", "new_text": ""}])
        self.assertEqual(fake.requests, [{"insertText": {"location": {"index": rows[4]["startIndex"], "tabId": "t.9"},
                                                         "text": "✅ "}}])



class BatchAndTab(unittest.TestCase):
    """auto_batches.json (the interviewer calls already used for a batch) sorts after every batch file, so a
    latest_batch() that took every *.json would read it as the newest batch, look up a tab "batches" and stop every
    hourly run on a bare StopIteration."""

    def test_latest_batch_is_the_newest_engine_batch_file(self):
        ns, _ = load()
        with tempfile.TemporaryDirectory() as d:
            ns["BATCHES"] = Path(d)
            for name in ("2026-11-02_Engine_Batch_9.json", "2026-11-02_Engine_Batch_10.json", "2026-11-01_Engine_Batch_8.json",
                         "2026-11-02_Engine_Batch_10.feedback.json", "2026-11-02_Engine_Batch_10_visuals.json",
                         "auto_batches.json"):
                (Path(d) / name).write_text("[]")
            self.assertEqual(ns["latest_batch"]().name, "2026-11-02_Engine_Batch_10.json")
            self.assertEqual([f.name for f in ns["batch_files"]()],
                             ["2026-11-01_Engine_Batch_8.json", "2026-11-02_Engine_Batch_9.json", "2026-11-02_Engine_Batch_10.json"])

    def test_tab_by_title_id_or_loose_title_and_a_clear_error(self):
        ns, _ = load()
        doc = {"tabs": [{"tabProperties": {"title": "Engine Batch 9", "tabId": "t.9"}},
                        {"tabProperties": {"title": "engine  batch_10", "tabId": "t.10"}}]}
        self.assertEqual(ns["find_tab"](doc, "Engine Batch 9")["tabProperties"]["tabId"], "t.9")
        self.assertEqual(ns["find_tab"](doc, "t.9")["tabProperties"]["tabId"], "t.9")
        self.assertEqual(ns["find_tab"](doc, "Engine Batch 10")["tabProperties"]["tabId"], "t.10")   # renamed by hand
        with self.assertRaises(ns["TabNotFound"]) as e:
            ns["find_tab"](doc, "batches")
        self.assertIn("'batches'", str(e.exception))
        self.assertIn("'Engine Batch 9'", str(e.exception))               # names the tabs there are

    def test_a_missing_tab_reads_no_edits_instead_of_crashing(self):
        ns, _ = load()
        ns["_docs"] = lambda: FakeDocs(batch_tab())
        self.assertEqual(ns["doc_edits"]("Engine Batch 404", [{"text": "x", "status": "approved"}]), [])


if __name__ == "__main__":
    unittest.main()
