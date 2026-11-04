"""BFF arguments that may be None must be declared optional.

pytincture checks every BFF argument against its annotation, as written, before the
method runs (pytincture.backend.bff_requests). A parameter declared `long: dict = None`
rejects the None the page sends, with a 400 and no error the user can see: that is
how the Memory modal's Save silently stopped working for scenario and
character-less chats.
"""
import ast
import pathlib
import unittest

from pytincture.backend.bff_requests import _value_matches_annotation

BFF = pathlib.Path(__file__).resolve().parents[1] / "appcode" / "bff"


def _methods():
    for path in sorted(BFF.glob("*.py")):
        tree = ast.parse(path.read_text())
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for fn in (n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                if fn.name.startswith("_"):
                    continue                 # __init__'s _user is the server's, not the page's
                yield path.name, cls.name, fn


class SignatureTest(unittest.TestCase):
    def test_every_none_default_accepts_none(self):
        bad = []
        for file, cls, fn in _methods():
            args = fn.args.args[-len(fn.args.defaults):] if fn.args.defaults else []
            pairs = list(zip(args, fn.args.defaults)) + list(zip(fn.args.kwonlyargs, fn.args.kw_defaults))
            for arg, default in pairs:
                if isinstance(default, ast.Constant) and default.value is None and arg.annotation:
                    ann = ast.unparse(arg.annotation)
                    if not _value_matches_annotation(None, ann):
                        bad.append(f"{file} {cls}.{fn.name}({arg.arg}: {ann} = None)")
        self.assertEqual(bad, [], "declare these `X | None`")

    def test_memory_save_accepts_no_long_term(self):
        fn = next(f for file, cls, f in _methods() if cls == "Memory" and f.name == "save")
        long = next(a for a in fn.args.args if a.arg == "long")
        self.assertTrue(_value_matches_annotation(None, ast.unparse(long.annotation)))


if __name__ == "__main__":
    unittest.main()
